#!/usr/bin/env python3
"""Converge Keycloak mail to one authenticated, current Admin Settings snapshot.

Credentials and HTTP bodies stay in memory. No subprocess, temporary credential
file, deployment SMTP fallback or raw upstream error is used by this helper.
"""

import base64
import errno
import hashlib
import hmac
import json
import math
import os
import socket
import sys
import time
from email.utils import parseaddr
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

MAX_RESPONSE_BYTES = 65536
MAX_REALM_RESPONSE_BYTES = 1048576
HTTP_TIMEOUT_SECONDS = 10
# Keycloak masks the stored password on read; this keyed digest stands in for it.
FINGERPRINT = "orisoSyncFingerprint"


class ReconcileError(Exception):
    """Only fixed, safe messages cross the process logging boundary."""

    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


class TriggerNotReady(ReconcileError):
    """Only a transient helper startup failure; never carries an upstream payload."""

    def __init__(self):
        super().__init__("SMTP_RECONCILE_TRIGGER_NOT_READY", 503)


def transient_connection(error):
    reason = error.reason if isinstance(error, URLError) else error
    return (isinstance(reason, (ConnectionError, TimeoutError))
            or isinstance(reason, socket.gaierror) and reason.errno == socket.EAI_AGAIN
            or isinstance(reason, OSError) and reason.errno in
            (errno.ENETUNREACH, errno.EHOSTUNREACH))


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

    def request(self, method, url, body=None, headers=None, failure="SOURCE_UNAVAILABLE",
                revision=False, authenticate_source=False, timeout=HTTP_TIMEOUT_SECONDS,
                trigger_request=False, max_response_bytes=MAX_RESPONSE_BYTES, admin_api=False):
        if (not isinstance(max_response_bytes, int) or isinstance(max_response_bytes, bool)
                or not 0 < max_response_bytes <= MAX_REALM_RESPONSE_BYTES):
            raise ReconcileError("SMTP_RECONCILE_CONFIGURATION_INVALID: response limit")
        try:
            request = Request(url, data=body, method=method, headers=headers or {})
            with self.opener.open(request, timeout=timeout) as response:
                status = response.status
                data = response.read(max_response_bytes + 1)
                source_revision = response.headers.get("X-Smtp-Revision")
        except HTTPError as error:
            error.close()
            if authenticate_source and error.code in (401, 403):
                raise ReconcileError("SMTP_RECONCILE_UNAUTHORIZED", 403) from None
            # 502/503 while an upstream is still starting is transient for callers
            # that opted in (mail locale hook); they retry within their own budget.
            if trigger_request and error.code in (502, 503):
                raise TriggerNotReady() from None
            raise ReconcileError("SMTP_RECONCILE_" + failure, error.code) from None
        except (URLError, OSError) as error:
            if trigger_request and transient_connection(error):
                raise TriggerNotReady() from None
            # Exception strings and response bodies may contain tokens/passwords.
            raise ReconcileError("SMTP_RECONCILE_" + failure) from None
        except ValueError:
            # Exception strings and response bodies may contain tokens/passwords.
            raise ReconcileError("SMTP_RECONCILE_" + failure) from None
        if len(data) > max_response_bytes:
            raise ReconcileError("SMTP_RECONCILE_" + failure + ": response too large")
        if status == 204 or (admin_api and status == 201):
            return (None, source_revision) if revision else None
        if status != 200:
            raise ReconcileError("SMTP_RECONCILE_" + failure, status)
        try:
            parsed = json.loads(data)
        except (ValueError, UnicodeError):
            raise ReconcileError("SMTP_RECONCILE_" + failure + ": invalid response") from None
        if not isinstance(parsed, dict) and not (admin_api and isinstance(parsed, list)):
            raise ReconcileError("SMTP_RECONCILE_" + failure + ": invalid response")
        return (parsed, source_revision) if revision else parsed

    def login(self, url, realm, client, username, password, failure,
              transient_startup=False):
        body = urlencode({"grant_type": "password", "client_id": client,
                          "username": username, "password": password}).encode()
        response = self.request(
            "POST", url + "/realms/" + quote(realm, safe="") + "/protocol/openid-connect/token",
            body, {"Content-Type": "application/x-www-form-urlencoded"}, failure,
            trigger_request=transient_startup,
        )
        token = response.get("access_token") if response else None
        if not isinstance(token, str) or not token.strip():
            raise ReconcileError("SMTP_RECONCILE_" + failure)
        return token

    def service_login(self, url, realm, client, secret, failure):
        body = urlencode({"grant_type": "client_credentials", "client_id": client,
                          "client_secret": secret}).encode()
        response = self.request(
            "POST", url + "/realms/" + quote(realm, safe="") + "/protocol/openid-connect/token",
            body, {"Content-Type": "application/x-www-form-urlencoded"}, failure)
        token = response.get("access_token") if response else None
        if not isinstance(token, str) or not token.strip():
            raise ReconcileError("SMTP_RECONCILE_" + failure)
        return token


def decode_claims(token):
    # Payload only; the issuer and the receiving service verify the signature.
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError()
    return json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))


def verify_technical_identity(token, subject, client):
    # This token was just issued by the configured Keycloak endpoint. The CTS
    # resource server independently verifies its signature and authority; these
    # checks prevent accidentally using a different configured service account.
    try:
        claims = decode_claims(token)
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


def configuration(env):
    names = ("KEYCLOAK_URL", "KEYCLOAK_REALM", "CONSULTING_TYPE_SERVICE_URL", "POD_NAMESPACE",
             "TECHNICAL_SERVICE_SUBJECT", "TECHNICAL_CLIENT_ID", "TECHNICAL_CLIENT_SECRET",
             "SMTP_SYNC_CLIENT_ID", "SMTP_SYNC_CLIENT_SECRET")
    config = {name: required(env, name) for name in names}
    # The sync client lives in the ORISO realm; master is never a valid target.
    if config["KEYCLOAK_REALM"] == "master":
        raise ReconcileError("SMTP_RECONCILE_CONFIGURATION_INVALID: protected realm")
    config["KEYCLOAK_URL"] = base_url(config["KEYCLOAK_URL"], "keycloak", config["POD_NAMESPACE"])
    config["CONSULTING_TYPE_SERVICE_URL"] = base_url(
        config["CONSULTING_TYPE_SERVICE_URL"], "consultingtypeservice", config["POD_NAMESPACE"])
    return config


def technical_login(http, config):
    technical_token = http.service_login(
        config["KEYCLOAK_URL"], config["KEYCLOAK_REALM"], config["TECHNICAL_CLIENT_ID"],
        config["TECHNICAL_CLIENT_SECRET"], "SOURCE_UNAVAILABLE",
    )
    verify_technical_identity(technical_token, config["TECHNICAL_SERVICE_SUBJECT"], config["TECHNICAL_CLIENT_ID"])
    return technical_token


def verify_sync_identity(token, client):
    # Fail with a clear code instead of a later 403 when the client lacks manage-realm.
    try:
        claims = decode_claims(token)
        expiry = claims.get("exp")
        roles = claims.get("resource_access", {}).get("realm-management", {}).get("roles")
        valid = (claims.get("azp") == client and isinstance(roles, list) and "manage-realm" in roles
                 and isinstance(expiry, (int, float)) and not isinstance(expiry, bool)
                 and math.isfinite(expiry) and expiry > time.time())
    except (ValueError, UnicodeError, AttributeError, TypeError):
        valid = False
    if not valid:
        raise ReconcileError("SMTP_RECONCILE_SYNC_IDENTITY_MISMATCH")


def sync_login(http, config):
    # client_credentials in the ORISO realm; no master-realm credential exists here.
    token = http.service_login(
        config["KEYCLOAK_URL"], config["KEYCLOAK_REALM"], config["SMTP_SYNC_CLIENT_ID"],
        config["SMTP_SYNC_CLIENT_SECRET"], "KEYCLOAK_UPDATE_FAILED",
    )
    verify_sync_identity(token, config["SMTP_SYNC_CLIENT_ID"])
    return token


def saved_revision(value):
    if (not isinstance(value, str) or not value.isascii() or not value.isdecimal()
            or len(value) > 19 or int(value) > 2**63 - 1):
        raise ReconcileError("SMTP_RECONCILE_SOURCE_REVISION_INVALID")
    return int(value)


def read_snapshot(http, config, technical_token):
    # Body and X-Smtp-Revision come from one saved document; the revision is what we acknowledge.
    snapshot, revision = http.request(
        "GET", config["CONSULTING_TYPE_SERVICE_URL"] + "/settingsadmin/smtp-credentials",
        headers={"Authorization": "Bearer " + technical_token, "tenantId": "0",
                 "Cache-Control": "no-store"}, revision=True)
    return snapshot, saved_revision(revision)


def acknowledge(http, config, technical_token, revision, status):
    # CTS marks only this exact revision; 409 means a newer save the next run applies.
    if revision == 0:
        return "SMTP_RECONCILE_NOTHING_SAVED"
    try:
        http.request("POST", config["CONSULTING_TYPE_SERVICE_URL"] + "/settingsadmin/smtp-sync-acknowledgement",
                     json.dumps({"revision": revision, "status": status}).encode(),
                     {"Authorization": "Bearer " + technical_token, "tenantId": "0",
                      "Content-Type": "application/json"}, "ACK_FAILED")
    except ReconcileError as error:
        if error.status == 409:
            return "SMTP_RECONCILE_ACK_STALE"
        if error.status in (404, 405):
            # Upgrade window: Keycloak is already written; only the Admin status lags.
            return ("WARNING SMTP_RECONCILE_ACK_UNSUPPORTED: CTS has no acknowledgement endpoint; status stays "
                    "pending until CTS feat/420-smtp-sync-ack is deployed")
        raise
    return "SMTP_RECONCILE_ACKNOWLEDGED"


def desired_smtp(transport, key):
    if not transport:
        return {}
    digest = hmac.new(key.encode(), json.dumps(transport, sort_keys=True).encode(), hashlib.sha256)
    return {**transport, FINGERPRINT: digest.hexdigest()}


def realm_holds(current, desired):
    if not isinstance(current, dict) or ("password" in current) != ("password" in desired):
        return False
    return ({k: v for k, v in current.items() if k != "password"}
            == {k: v for k, v in desired.items() if k != "password"})


def apply_snapshot(http, config, snapshot):
    transport = smtp_transport(snapshot)
    admin_token = sync_login(http, config)
    desired = desired_smtp(transport, config["SMTP_SYNC_CLIENT_SECRET"])
    realm_url = config["KEYCLOAK_URL"] + "/admin/realms/" + quote(config["KEYCLOAK_REALM"], safe="")
    realm = http.request("GET", realm_url, headers={"Authorization": "Bearer " + admin_token},
                         failure="KEYCLOAK_UPDATE_FAILED", max_response_bytes=MAX_REALM_RESPONSE_BYTES)
    status = "APPLIED" if transport else "DISABLED_OR_INCOMPLETE"
    if realm_holds(realm.get("smtpServer"), desired):
        return status, False
    http.request("PUT", realm_url, json.dumps({"smtpServer": desired}).encode(),
                 {"Authorization": "Bearer " + admin_token, "Content-Type": "application/json"},
                 "KEYCLOAK_UPDATE_FAILED")
    return status, True


def reconcile(env):
    config = configuration(env)
    http = HttpClient()
    technical_token = technical_login(http, config)
    snapshot, revision = read_snapshot(http, config, technical_token)
    status, written = apply_snapshot(http, config, snapshot)
    if not written:
        print("SMTP_RECONCILE_UNCHANGED: Keycloak already holds the current Admin Settings")
    elif status == "APPLIED":
        print("SMTP_RECONCILE_APPLIED: current Admin Settings")
    else:
        print("SMTP_DISABLED_OR_INCOMPLETE: Keycloak mail disabled until Admin Settings are complete")
    print(acknowledge(http, config, technical_token, revision, status))


def main():
    try:
        if not sys.argv[1:]:
            reconcile(os.environ)
        else:
            raise ReconcileError("SMTP_RECONCILE_CONFIGURATION_INVALID: mode")
    except ReconcileError as error:
        print(str(error), file=sys.stderr)
        return 2
    except Exception:
        # Do not leak a traceback, an HTTP reply or a credential on an unplanned
        # failure. The next CronJob run retries.
        print("SMTP_RECONCILE_FAILED", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
