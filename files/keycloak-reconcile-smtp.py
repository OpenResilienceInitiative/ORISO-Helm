#!/usr/bin/env python3
"""Converge Keycloak mail to one authenticated, current Admin Settings snapshot.

Credentials and HTTP bodies stay in memory. No subprocess, temporary credential
file, deployment SMTP fallback or raw upstream error is used by this helper.
"""

import base64
import json
import math
import os
import sys
import time
from email.utils import parseaddr
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

MAX_RESPONSE_BYTES = 65536
HTTP_TIMEOUT_SECONDS = 10


class ReconcileError(Exception):
    """Only fixed, safe messages cross the process logging boundary."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, _request, _response, status, _message, headers, _location):
        # HttpClient assigns the safe error to the source or update operation.
        raise HTTPError("", status, "redirect refused", headers, None)


def required(env, name):
    value = env.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ReconcileError("SMTP_RECONCILE_CONFIGURATION_MISSING: " + name)
    return value


def base_url(value, service, namespace):
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ReconcileError("SMTP_RECONCILE_CONFIGURATION_INVALID: service URL")
    try:
        url = urlsplit(value)
        port = url.port
    except ValueError:
        raise ReconcileError("SMTP_RECONCILE_CONFIGURATION_INVALID: service URL") from None
    if (url.scheme not in ("https", "http") or not url.hostname or url.username
            or url.password or url.query or url.fragment or port == 0):
        raise ReconcileError("SMTP_RECONCILE_CONFIGURATION_INVALID: service URL")
    # The established Helm services use cluster-internal HTTP. Never downgrade
    # TLS or send credentials to a configured public HTTP host.
    internal_hosts = {service, service + "." + namespace, service + "." + namespace + ".svc",
                      service + "." + namespace + ".svc.cluster.local", "localhost", "127.0.0.1", "::1"}
    if url.scheme == "http" and url.hostname not in internal_hosts:
        raise ReconcileError("SMTP_RECONCILE_CONFIGURATION_INVALID: public HTTP service URL")
    return value.rstrip("/")


class HttpClient:
    def __init__(self):
        # urllib's HTTPS handler verifies the normal trust store and host name.
        # No redirects/proxy credentials/disabled-certificate fallback are used.
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, method, url, body=None, headers=None, failure="SOURCE_UNAVAILABLE"):
        try:
            request = Request(url, data=body, method=method, headers=headers or {})
            with self.opener.open(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                status = response.status
                data = response.read(MAX_RESPONSE_BYTES + 1)
        except (HTTPError, URLError, OSError, ValueError):
            # Exception strings and response bodies may contain tokens/passwords.
            raise ReconcileError("SMTP_RECONCILE_" + failure) from None
        if len(data) > MAX_RESPONSE_BYTES:
            raise ReconcileError("SMTP_RECONCILE_" + failure + ": response too large")
        if status == 204:
            return None
        if status != 200:
            raise ReconcileError("SMTP_RECONCILE_" + failure)
        try:
            parsed = json.loads(data)
        except (ValueError, UnicodeError):
            raise ReconcileError("SMTP_RECONCILE_" + failure + ": invalid response") from None
        if not isinstance(parsed, dict):
            raise ReconcileError("SMTP_RECONCILE_" + failure + ": invalid response")
        return parsed

    def login(self, url, realm, client, username, password, failure):
        body = urlencode({"grant_type": "password", "client_id": client,
                          "username": username, "password": password}).encode()
        response = self.request(
            "POST", url + "/realms/" + quote(realm, safe="") + "/protocol/openid-connect/token",
            body, {"Content-Type": "application/x-www-form-urlencoded"}, failure,
        )
        token = response.get("access_token") if response else None
        if not isinstance(token, str) or not token.strip():
            raise ReconcileError("SMTP_RECONCILE_" + failure)
        return token


def verify_technical_identity(token, subject, client):
    # This token was just issued by the configured Keycloak endpoint. The CTS
    # resource server independently verifies its signature and authority; these
    # checks prevent accidentally using a different configured service account.
    try:
        parts = token.split(".")
        if len(parts) != 3:
            raise ValueError()
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        expiry = claims.get("exp")
        roles = claims.get("realm_access", {}).get("roles")
        valid = (claims.get("sub") == subject and claims.get("azp") == client
                 and isinstance(roles, list) and "technical" in roles
                 and isinstance(expiry, (int, float)) and not isinstance(expiry, bool)
                 and math.isfinite(expiry) and expiry > time.time())
    except (ValueError, UnicodeError, AttributeError, TypeError):
        valid = False
    if not valid:
        raise ReconcileError("SMTP_RECONCILE_TECHNICAL_IDENTITY_MISMATCH")


def smtp_transport(snapshot):
    if snapshot is None:
        return {}
    if (snapshot.get("globalSmtpEnabled") is not True
            or snapshot.get("globalFeatureSystemNotificationEmailsEnabled") is not True):
        return {}
    names = ("Host", "Port", "From", "Username", "Password")
    values = [snapshot.get("globalSmtp" + name) for name in names]
    if any(not isinstance(v, str) or not v.strip() for v in values):
        return {}
    # SMTP credentials are opaque strings. JSON encodes their control bytes;
    # only host/port/from need field validation, never password normalization.
    if any(any(ord(c) < 32 or ord(c) == 127 for c in v) for v in values[:3]):
        return {}
    host, port, sender, username, password = values
    host, port = host.strip(), port.strip()
    secure = snapshot.get("globalSmtpSecure")
    if (not isinstance(secure, bool) or len(port) > 5 or not port.isascii() or not port.isdecimal()
            or not 1 <= int(port) <= 65535 or "/" in host or any(c.isspace() for c in host)):
        return {}
    display_name, address = parseaddr(sender, strict=True)
    if not address or address.count("@") != 1 or not all(address.split("@")):
        return {}
    return {"host": host, "port": str(int(port)), "ssl": str(secure).lower(),
            "starttls": str(not secure).lower(), "auth": "true", "from": address,
            "fromDisplayName": display_name, "user": username, "password": password}


def reconcile(env):
    names = ("KEYCLOAK_URL", "KEYCLOAK_REALM", "CONSULTING_TYPE_SERVICE_URL", "POD_NAMESPACE",
             "TECHNICAL_USERNAME", "TECHNICAL_PASSWORD", "TECHNICAL_SERVICE_SUBJECT",
             "TECHNICAL_CLIENT_ID", "KEYCLOAK_ADMIN_USERNAME", "KEYCLOAK_ADMIN_PASSWORD")
    config = {name: required(env, name) for name in names}
    kc = base_url(config["KEYCLOAK_URL"], "keycloak", config["POD_NAMESPACE"])
    cts = base_url(config["CONSULTING_TYPE_SERVICE_URL"], "consultingtypeservice", config["POD_NAMESPACE"])
    http = HttpClient()
    technical_token = http.login(
        kc, config["KEYCLOAK_REALM"], config["TECHNICAL_CLIENT_ID"],
        config["TECHNICAL_USERNAME"], config["TECHNICAL_PASSWORD"], "SOURCE_UNAVAILABLE",
    )
    verify_technical_identity(technical_token, config["TECHNICAL_SERVICE_SUBJECT"], config["TECHNICAL_CLIENT_ID"])
    # This platform-scoped read is independent of an end-user token or tenant.
    snapshot = http.request("GET", cts + "/settingsadmin/smtp-credentials",
                            headers={"Authorization": "Bearer " + technical_token, "tenantId": "0",
                                     "Cache-Control": "no-store"})
    transport = smtp_transport(snapshot)
    admin_token = http.login(
        kc, "master", "admin-cli", config["KEYCLOAK_ADMIN_USERNAME"],
        config["KEYCLOAK_ADMIN_PASSWORD"], "KEYCLOAK_UPDATE_FAILED",
    )
    http.request("PUT", kc + "/admin/realms/" + quote(config["KEYCLOAK_REALM"], safe=""),
                 json.dumps({"smtpServer": transport}).encode(),
                 {"Authorization": "Bearer " + admin_token, "Content-Type": "application/json"},
                 "KEYCLOAK_UPDATE_FAILED")
    if transport:
        print("SMTP_RECONCILE_APPLIED: current Admin Settings")
    else:
        print("SMTP_DISABLED_OR_INCOMPLETE: Keycloak mail disabled until Admin Settings are complete")


def main():
    try:
        reconcile(os.environ)
    except ReconcileError as error:
        print(str(error), file=sys.stderr)
        return 2
    except Exception:
        # Do not leak a traceback, an HTTP reply or a credential on an unplanned
        # failure. Kubernetes retries the job; the next poll reads a fresh source.
        print("SMTP_RECONCILE_FAILED", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
