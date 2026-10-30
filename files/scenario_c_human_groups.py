#!/usr/bin/env python3
"""Opt-in human role-group migration for a quiescent Keycloak realm copy.

Dry-run by default. Reads an explicit, reviewed mapping; does not enable native
permissions. Supply a short-lived admin token through ORISO_KEYCLOAK_ADMIN_TOKEN.
Reports contain IDs and role/group names, never tokens or user profile fields.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
from http.client import HTTPException


class MigrationError(Exception):
    pass


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise MigrationError("Admin API redirect refused")


class Admin:
    def __init__(self, url, realm, token):
        self.base = url.rstrip("/") + "/admin/realms/" + quote(realm, safe="")
        self.token = token
        self.opener = build_opener(NoRedirects())

    def request(self, method, path, body=None, missing=False):
        data = None if body is None else json.dumps(body).encode()
        request = Request(
            self.base + path,
            data=data,
            method=method,
            headers={
                "Authorization": "Bearer " + self.token,
                "Content-Type": "application/json",
            },
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except HTTPError as error:
            status = error.code
            error.close()
            if missing and status == 404:
                return None
            raise MigrationError(f"{method} Admin API returned HTTP {status}") from None
        except (URLError, ValueError, OSError, HTTPException):
            raise MigrationError(f"{method} Admin API request failed") from None

    def paged(self, path, query=None):
        rows = []
        first = 0
        while True:
            batch = self.request(
                "GET",
                path + "?" + urlencode({**(query or {}), "first": first, "max": 100}),
            )
            rows.extend(batch)
            if len(batch) < 100:
                return rows
            first += len(batch)

    def snapshot(self, uid, clients):
        root = "/users/" + quote(uid, safe="")
        realm = self.request("GET", root + "/role-mappings/realm/composite")
        client_roles = {}
        for client in clients:
            roles = self.request(
                "GET",
                root
                + "/role-mappings/clients/"
                + quote(client["id"], safe="")
                + "/composite",
            )
            if roles:
                client_roles[client["id"]] = sorted(r["name"] for r in roles)
        return {"realm": sorted(r["name"] for r in realm), "clients": client_roles}


def application_roles(snapshot, realm):
    return set(snapshot["realm"]) - {
        "offline_access",
        "uma_authorization",
        "default-roles-" + realm,
    }


PROTECTED_ROLES = {
    "tenant-admin",
    "single-tenant-admin",
    "topic-admin",
    "global-support-admin",
}
TECHNICAL_ROLES = {
    "technical",
    "notifications-technical",
    "otp-config-admin",
    "config-wizard",
    "invitation-reservations",
    "consultant-import",
    "account-provisioning",
    "account-read",
    "account-maintenance",
    "session-exchange",
    "smtp-sync",
    "system-email-delivery",
    "notification-dispatch",
    "runtime-policy",
    "matrix-agency",
    "matrix-agency-provision",
    "appointment-sync",
    "appointment-participant-cleanup",
}


def validate_mapping(mapping):
    if (
        not isinstance(mapping, dict)
        or mapping.get("version") != 1
        or not isinstance(mapping.get("groups"), list)
        or not mapping["groups"]
    ):
        raise MigrationError("Mapping must have version 1 and a nonempty groups list")
    paths = set()
    combinations = set()
    for group in mapping["groups"]:
        if not isinstance(group, dict):
            raise MigrationError("Each mapping must be an object")
        path, roles = group.get("path"), group.get("roles")
        if (
            not isinstance(path, str)
            or not isinstance(roles, list)
            or not roles
            or any(not isinstance(r, str) or not r for r in roles)
        ):
            raise MigrationError("Each mapping needs a path and nonempty role names")
        segments = path.split("/")
        ordinary = (
            len(segments) == 3
            and segments[0] == ""
            and segments[1] == "human-roles"
            and bool(segments[2])
        )
        protected = (
            len(segments) == 3
            and segments[0] == ""
            and segments[1] == "protected-admins"
            and bool(segments[2])
            and segments[2] != "technical-identities"
        )
        if not (ordinary or protected) or any(p in (".", "..") for p in segments):
            raise MigrationError("Use canonical human-roles or protected-admins paths")
        if set(roles) & PROTECTED_ROLES and not protected:
            raise MigrationError("Administrative roles require a protected group")
        if (
            set(roles) & TECHNICAL_ROLES
            or any(r.startswith(("oriso-task-", "default-roles-")) for r in roles)
            or set(roles) & {"offline_access", "uma_authorization"}
        ):
            raise MigrationError(
                "Technical and Keycloak baseline roles cannot be human-role mappings"
            )
        combination = tuple(sorted(roles))
        if (
            path in paths
            or combination in combinations
            or len(set(roles)) != len(roles)
        ):
            raise MigrationError(
                "Group paths and exact role combinations must be unique"
            )
        paths.add(path)
        combinations.add(combination)


def is_role_group(path):
    return path in ("/human-roles", "/protected-admins") or path.startswith(
        ("/human-roles/", "/protected-admins/")
    )


def verify_target(admin, uid, target, clients):
    root = "/users/" + quote(uid, safe="")
    memberships = admin.paged(root + "/groups")
    role_memberships = [g for g in memberships if is_role_group(g["path"])]
    if (
        any(g["path"] != target["path"] for g in role_memberships)
        or len(role_memberships) > 1
    ):
        raise MigrationError(
            "Conflicting role-group membership; no automatic reclassification"
        )
    for role in target["roles"]:
        admin.request("GET", "/roles/" + quote(role, safe=""))
    group = get_group(admin, target["path"])
    roles, client_roles = group_grants(admin, target["path"], clients)
    if client_roles or (roles != set(target["roles"]) if group else bool(roles)):
        raise MigrationError("Target group grants differ from the reviewed mapping")
    return memberships


def get_group(admin, path):
    return admin.request(
        "GET", "/group-by-path/" + quote(path.lstrip("/"), safe=""), missing=True
    )


def group_grants(admin, path, clients):
    roles = set()
    client_roles = set()
    parts = path.strip("/").split("/")
    for depth in range(1, len(parts) + 1):
        group = get_group(admin, "/" + "/".join(parts[:depth]))
        if not group:
            continue
        root = "/groups/" + quote(group["id"], safe="") + "/role-mappings"
        roles.update(r["name"] for r in admin.request("GET", root + "/realm/composite"))
        for client in clients:
            assigned = admin.request(
                "GET", root + "/clients/" + quote(client["id"], safe="") + "/composite"
            )
            client_roles.update((client["id"], r["name"]) for r in assigned)
    return roles, client_roles


def ensure_group(admin, target, clients):
    path = target["path"]
    parts = path.strip("/").split("/")
    group = get_group(admin, path)
    if group:
        roles, client_roles = group_grants(admin, path, clients)
        if roles != set(target["roles"]) or client_roles:
            raise MigrationError("Target group grants differ from the reviewed mapping")
        return group
    parent = None
    for depth, part in enumerate(parts, 1):
        current_path = "/" + "/".join(parts[:depth])
        current = get_group(admin, current_path)
        if current is None:
            create = (
                "/groups"
                if parent is None
                else "/groups/" + quote(parent["id"], safe="") + "/children"
            )
            admin.request("POST", create, {"name": part})
            current = get_group(admin, current_path)
            if current is None:
                raise MigrationError("Created group could not be read back")
        # A parent with roles would widen membership for all children.
        roles, client_roles = group_grants(admin, current_path, clients)
        if roles or client_roles:
            raise MigrationError(
                "Role-group ancestry already carries unexpected grants"
            )
        parent = current
    roles = [
        admin.request("GET", "/roles/" + quote(r, safe="")) for r in target["roles"]
    ]
    admin.request(
        "POST",
        "/groups/" + quote(parent["id"], safe="") + "/role-mappings/realm",
        roles,
    )
    grants, client_roles = group_grants(admin, path, clients)
    if grants != set(target["roles"]) or client_roles:
        raise MigrationError("New group grants could not be verified")
    return parent


def migrate_user(admin, uid, target, clients, before):
    root = "/users/" + quote(uid, safe="")
    memberships = verify_target(admin, uid, target, clients)
    group = ensure_group(admin, target, clients)
    direct = admin.request("GET", root + "/role-mappings/realm")
    removable = [r for r in direct if r["name"] in target["roles"]]
    already_joined = any(g["path"] == target["path"] for g in memberships)
    membership = root + "/groups/" + quote(group["id"], safe="")
    changed_direct = False
    try:
        if not already_joined:
            admin.request("PUT", membership)
        groups = admin.paged(root + "/groups")
        if (
            not any(g["id"] == group["id"] for g in groups)
            or admin.snapshot(uid, clients) != before
        ):
            raise MigrationError(
                "Membership or effective-role readback failed before direct-role removal"
            )
        if removable:
            # Treat a lost DELETE response as possibly committed; restore on any failure.
            changed_direct = True
            admin.request("DELETE", root + "/role-mappings/realm", removable)
        final = admin.snapshot(uid, clients)
        remaining = admin.request("GET", root + "/role-mappings/realm")
        groups = admin.paged(root + "/groups")
        role_groups = [g for g in groups if is_role_group(g["path"])]
        if (
            final != before
            or any(r["name"] in target["roles"] for r in remaining)
            or [g["path"] for g in role_groups] != [target["path"]]
        ):
            raise MigrationError("Final effective-role or direct-role readback failed")
        return "migrated" if removable or not already_joined else "unchanged"
    except MigrationError as error:
        # Restore first; retaining group membership is safer than dropping the last grant.
        try:
            if changed_direct:
                admin.request("POST", root + "/role-mappings/realm", removable)
            if not already_joined:
                admin.request("DELETE", membership)
            if admin.snapshot(uid, clients) != before:
                raise MigrationError("Restored effective roles differ")
        except MigrationError:
            raise MigrationError(
                "Migration failed; recovery incomplete, stop and inspect this user"
            ) from None
        raise MigrationError(
            str(error) + "; original effective roles restored"
        ) from None


def migrate(admin, realm, mapping, apply=False):
    clients = admin.paged("/clients")
    # The native unfiltered users listing excludes service accounts by their
    # model link, even when renamed. Listing representations do not expose that
    # link. Never use service-account-user GET here: it can create missing users.
    users = admin.paged("/users")
    report = {
        "mode": "apply" if apply else "dry-run",
        "users": [],
        "technicalClients": [
            {"id": client["id"], "clientId": client["clientId"], "status": "preserved"}
            for client in clients
            if client.get("serviceAccountsEnabled")
        ],
    }
    for user in users:
        uid = user["id"]
        entry = {"id": uid}
        if user.get("serviceAccountClientId"):
            entry["status"] = "service-account-skipped"
            report["users"].append(entry)
            continue
        try:
            snapshot = admin.snapshot(uid, clients)
            direct = admin.request(
                "GET", "/users/" + quote(uid, safe="") + "/role-mappings"
            )
            if any(
                c.get("mappings") for c in direct.get("clientMappings", {}).values()
            ):
                entry.update(
                    status="unclassifiable", reason="direct-client-role-mappings"
                )
                report["users"].append(entry)
                continue
            account_clients = {
                c["id"] for c in clients if c.get("clientId") == "account"
            }
            if any(
                cid not in account_clients
                or set(roles) - {"manage-account", "view-profile"}
                for cid, roles in snapshot["clients"].items()
            ):
                entry.update(
                    status="unclassifiable",
                    reason="unclassified-effective-client-roles",
                )
                report["users"].append(entry)
                continue
            target = next(
                (
                    g
                    for g in mapping["groups"]
                    if set(g["roles"]) == application_roles(snapshot, realm)
                ),
                None,
            )
            if target:
                entry["group"] = target["path"]
                if apply:
                    entry["status"] = migrate_user(
                        admin, uid, target, clients, snapshot
                    )
                else:
                    verify_target(admin, uid, target, clients)
                    entry["status"] = "would-migrate"
            else:
                entry.update(status="unclassifiable", roles=snapshot["realm"])
        except MigrationError as error:
            entry.update(status="failed", error=str(error))
            report["users"].append(entry)
            break
        report["users"].append(entry)
    report["scanned"] = len(report["users"])
    report["total"] = len(users)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url", required=True, help="Keycloak base URL including /auth when configured"
    )
    parser.add_argument("--realm", required=True)
    parser.add_argument(
        "--mapping", required=True, help="Reviewed JSON role-combination mapping"
    )
    parser.add_argument(
        "--apply", action="store_true", help="Explicitly migrate; default is read-only"
    )
    args = parser.parse_args()
    try:
        url = urlparse(args.url)
        if url.scheme != "https" and not (
            url.scheme == "http" and url.hostname in ("127.0.0.1", "localhost", "::1")
        ):
            raise MigrationError(
                "Use HTTPS, or loopback HTTP for an isolated test realm"
            )
        if url.username or url.password or url.query or url.fragment:
            raise MigrationError(
                "Keycloak URL must not contain credentials, query or fragment"
            )
        token = os.environ.get("ORISO_KEYCLOAK_ADMIN_TOKEN")
        if not token:
            raise MigrationError("ORISO_KEYCLOAK_ADMIN_TOKEN is required")
        with open(args.mapping) as source:
            mapping = json.load(source)
        validate_mapping(mapping)
        report = migrate(
            Admin(args.url, args.realm, token), args.realm, mapping, args.apply
        )
        print(json.dumps(report, sort_keys=True))
        return (
            2
            if any(u["status"] == "failed" for u in report["users"])
            else (
                3
                if any(u["status"] == "unclassifiable" for u in report["users"])
                else 0
            )
        )
    except (MigrationError, OSError, ValueError) as error:
        message = (
            str(error) if isinstance(error, MigrationError) else "Invalid mapping file"
        )
        print(
            json.dumps(
                {
                    "mode": "apply" if args.apply else "dry-run",
                    "error": message,
                    "users": [],
                }
            )
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
