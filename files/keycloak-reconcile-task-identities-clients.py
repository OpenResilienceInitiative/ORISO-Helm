#!/usr/bin/env python3
"""Converge five task-identity Keycloak clients without touching human users.

The provision pass creates disabled clients and prints their generated service-account
subjects. The enable pass pins those subjects from Helm values before enabling tokens.
"""
import importlib.util
import math
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import quote


spec = importlib.util.spec_from_file_location(
    "service_clients", Path(__file__).with_name("keycloak-reconcile-service-clients.py")
)
service_clients = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service_clients)
smtp = service_clients.smtp
Error = service_clients.Error


ACTORS = (
    ("SETUP", "backend-setup", "TASK_IDENTITY_SETUP_CLIENT_SECRET", "TASK_IDENTITY_SETUP_SERVICE_SUBJECT",
     ("config-wizard", "invitation-reservations", "consultant-import"),
     ("tenantservice", "agencyservice", "consultingtypeservice", "userservice")),
    ("ACCOUNT_CREATE", "backend-account-create", "TASK_IDENTITY_ACCOUNT_CREATE_CLIENT_SECRET", "TASK_IDENTITY_ACCOUNT_CREATE_SERVICE_SUBJECT",
     ("account-provisioning", "account-read"), ()),
    ("ACCOUNT_ADMIN", "backend-account-admin", "TASK_IDENTITY_ACCOUNT_ADMIN_CLIENT_SECRET", "TASK_IDENTITY_ACCOUNT_ADMIN_SERVICE_SUBJECT",
     ("account-maintenance", "account-read", "otp-config-admin", "session-exchange"), ()),
    ("MAIL", "backend-mail", "TASK_IDENTITY_MAIL_CLIENT_SECRET", "TASK_IDENTITY_MAIL_SERVICE_SUBJECT",
     ("smtp-sync", "system-email-delivery", "notification-dispatch", "notifications-technical"),
     ("tenantservice", "agencyservice", "consultingtypeservice", "userservice")),
    ("RUNTIME", "backend-runtime", "TASK_IDENTITY_RUNTIME_CLIENT_SECRET", "TASK_IDENTITY_RUNTIME_SERVICE_SUBJECT",
     ("runtime-policy", "matrix-agency", "matrix-agency-provision", "appointment-sync", "appointment-participant-cleanup", "deletion-audit-read"),
     ("tenantservice", "agencyservice", "appointmentservice")),
)
RECEIPT = "oriso-task-identities-client"


def required(env, name):
    return smtp.required(env, name)


def configuration(env):
    names = ("KEYCLOAK_URL", "KEYCLOAK_REALM", "POD_NAMESPACE", "KEYCLOAK_ADMIN_USERNAME", "KEYCLOAK_ADMIN_PASSWORD", "TASK_IDENTITY_MODE")
    config = {name: required(env, name) for name in names}
    config["KEYCLOAK_URL"] = smtp.base_url(config["KEYCLOAK_URL"], "keycloak", config["POD_NAMESPACE"])
    if config["KEYCLOAK_REALM"] == "master" or config["TASK_IDENTITY_MODE"] not in ("provision", "enable"):
        raise Error("TASK_IDENTITY_CONFIGURATION_INVALID: mode or protected realm")
    secrets = []
    subjects = []
    for key, client_id, secret_name, subject_name, _, _ in ACTORS:
        secret = required(env, secret_name)
        if len(secret) < 32 or not all(32 <= ord(char) < 127 for char in secret):
            raise Error("TASK_IDENTITY_CONFIGURATION_INVALID: client secret")
        config[secret_name] = secret
        secrets.append(secret)
        if config["TASK_IDENTITY_MODE"] == "enable":
            subject = required(env, subject_name)
            if not re.fullmatch(r"[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}", subject):
                raise Error("TASK_IDENTITY_CONFIGURATION_INVALID: service subject")
            config[subject_name] = subject
            subjects.append(subject)
    if len(set(secrets)) != len(secrets) or (subjects and len(set(subjects)) != len(subjects)):
        raise Error("TASK_IDENTITY_CONFIGURATION_INVALID: actor bindings must be distinct")
    return config


class Admin(service_clients.Admin):
    def ensure_role(self, name):
        matches = [role for role in self.request("GET", "roles") if role.get("name") == name]
        if len(matches) > 1:
            raise Error("TASK_IDENTITY_ROLE_INVALID: ambiguous role")
        if not matches:
            self.request("POST", "roles", {"name": name, "composite": False})
            matches = [role for role in self.request("GET", "roles") if role.get("name") == name]
        if len(matches) != 1 or matches[0].get("composite"):
            raise Error("TASK_IDENTITY_ROLE_INVALID: role must be direct")
        return matches[0]

    def ensure_client(self, client_id, enabled):
        client = self.find_client(client_id)
        if client:
            self.validate_client(client)
            if client.get("attributes", {}).get(RECEIPT) != "v1":
                raise Error("TASK_IDENTITY_CLIENT_INVALID: unmanaged actor adoption is refused")
            return client
        self.request("POST", "clients", {
            "clientId": client_id,
            "protocol": "openid-connect",
            "enabled": enabled,
            "publicClient": False,
            "bearerOnly": False,
            "serviceAccountsEnabled": True,
            "directAccessGrantsEnabled": False,
            "standardFlowEnabled": False,
            "implicitFlowEnabled": False,
            "fullScopeAllowed": False,
            "attributes": {RECEIPT: "v1"},
        })
        client = self.find_client(client_id)
        if not client:
            raise Error("TASK_IDENTITY_CLIENT_INVALID: client creation failed")
        return client

    def replace_protocol_mappers(self, client, audiences):
        cid = quote(client["id"], safe="")
        for mapper in self.request("GET", "clients/" + cid + "/protocol-mappers/models"):
            self.request("DELETE", "clients/" + cid + "/protocol-mappers/models/" + quote(mapper["id"], safe=""))
        mappers = [{
            "name": "oriso-task-identities-realm-roles",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-usermodel-realm-role-mapper",
            "config": {
                "claim.name": "realm_access.roles", "multivalued": "true", "jsonType.label": "String",
                "access.token.claim": "true", "id.token.claim": "false", "usermodel.realmRoleMapping.rolePrefix": "",
            },
        }]
        mappers.extend({
            "name": "oriso-task-identities-audience-" + audience,
            "protocol": "openid-connect",
            "protocolMapper": "oidc-audience-mapper",
            "config": {"included.custom.audience": audience, "access.token.claim": "true", "id.token.claim": "false"},
        } for audience in audiences)
        for mapper in mappers:
            self.request("POST", "clients/" + cid + "/protocol-mappers/models", mapper)

    def configure_actor(self, client, subject, secret, roles, audiences, enabled):
        cid, uid = quote(client["id"], safe=""), quote(subject, safe="")
        for group in self.request("GET", "users/" + uid + "/groups"):
            self.request("DELETE", "users/" + uid + "/groups/" + quote(group["id"], safe=""))
        self.replace_mappings("users/" + uid + "/role-mappings", roles, [], "")
        self.replace_mappings("clients/" + cid + "/scope-mappings", roles, [], "")
        for scope_kind in ("default-client-scopes", "optional-client-scopes"):
            for scope in self.request("GET", "clients/" + cid + "/" + scope_kind):
                self.request("DELETE", "clients/" + cid + "/" + scope_kind + "/" + quote(scope["id"], safe=""))
        self.replace_protocol_mappers(client, audiences)
        self.request("PUT", "clients/" + cid, {
            "clientId": client["clientId"], "enabled": enabled, "publicClient": False, "bearerOnly": False,
            "serviceAccountsEnabled": True, "directAccessGrantsEnabled": False, "standardFlowEnabled": False,
            "implicitFlowEnabled": False, "fullScopeAllowed": False, "secret": secret,
            "attributes": {RECEIPT: "v1"},
        })


def verify_token(api, config, client_id, secret, subject, roles, audiences):
    token = api.http.service_login(config["KEYCLOAK_URL"], config["KEYCLOAK_REALM"], client_id, secret, "TASK_IDENTITY_TOKEN_UNAVAILABLE")
    try:
        claims = smtp.decode_claims(token)
        expiry = claims.get("exp")
        actual_audiences = claims.get("aud", [])
        actual_audiences = {actual_audiences} if isinstance(actual_audiences, str) else set(actual_audiences)
        valid = (claims.get("sub") == subject and claims.get("azp") == client_id
                 and isinstance(expiry, (int, float)) and not isinstance(expiry, bool)
                 and math.isfinite(expiry) and expiry > time.time()
                 and set(claims.get("realm_access", {}).get("roles", [])) == set(role["name"] for role in roles)
                 and actual_audiences == set(audiences)
                 and not claims.get("resource_access"))
    except (ValueError, UnicodeError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise Error("TASK_IDENTITY_TOKEN_CONTRACT_MISMATCH")


def reconcile(config):
    api = Admin(config)
    role_cache = {}
    subjects = {}
    for key, client_id, secret_name, subject_name, role_names, audiences in ACTORS:
        roles = [role_cache.setdefault(name, api.ensure_role(name)) for name in role_names]
        # A missing client is never created live. The enable pass must first
        # prove the persisted subject pin matches Keycloak's generated account.
        client = api.ensure_client(client_id, False)
        subject = api.subject(client)
        if config["TASK_IDENTITY_MODE"] == "enable" and subject != config[subject_name]:
            raise Error("TASK_IDENTITY_SUBJECT_MISMATCH: update Helm values from the provision output")
        api.configure_actor(client, subject, config[secret_name], roles, audiences, config["TASK_IDENTITY_MODE"] == "enable")
        subjects[key] = subject
        if config["TASK_IDENTITY_MODE"] == "enable":
            verify_token(api, config, client_id, config[secret_name], subject, roles, audiences)
    for key in ("SETUP", "ACCOUNT_CREATE", "ACCOUNT_ADMIN", "MAIL", "RUNTIME"):
        print("TASK_IDENTITY_" + key + "_SERVICE_SUBJECT=" + subjects[key])
    print("TASK_IDENTITY_CLIENTS_" + ("PROVISIONED" if config["TASK_IDENTITY_MODE"] == "provision" else "ENABLED"))


def main():
    try:
        reconcile(configuration(os.environ))
        return 0
    except Error as error:
        print(str(error), file=sys.stderr)
        return 2
    except Exception:
        print("TASK_IDENTITY_CLIENT_RECONCILE_FAILED", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
