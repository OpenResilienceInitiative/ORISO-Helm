#!/usr/bin/env python3
"""Converge Keycloak mail to one authenticated, current Admin Settings snapshot.

Credentials and HTTP bodies stay in memory. No subprocess, temporary credential
file, deployment SMTP fallback or raw upstream error is used by this helper.
"""

import base64
import errno
import json
import math
import os
import signal
import socket
import socketserver
import sys
import threading
import time
from email.utils import parseaddr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

MAX_RESPONSE_BYTES = 65536
MAX_REALM_RESPONSE_BYTES = 1048576
HTTP_TIMEOUT_SECONDS = 10
TRIGGER_READY_SECONDS = 120  # Bounded below the Helm Job's 600-second active deadline.
TRIGGER_RETRY_SECONDS = 5


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
            # 502 is the server reporting that Admin Settings/CTS is not
            # answering yet. A post-upgrade hook routinely runs while that
            # Deployment is still rolling, so it is as transient as the 503 the
            # server returns for itself: both wait out TRIGGER_READY_SECONDS.
            if trigger_request and error.code in (502, 503):
                raise TriggerNotReady() from None
            raise ReconcileError("SMTP_RECONCILE_" + failure) from None
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
            raise ReconcileError("SMTP_RECONCILE_" + failure)
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


def configuration(env, mode="reconcile"):
    names = ("KEYCLOAK_URL", "KEYCLOAK_REALM", "CONSULTING_TYPE_SERVICE_URL", "POD_NAMESPACE",
             "TECHNICAL_SERVICE_SUBJECT", "TECHNICAL_CLIENT_ID")
    if mode != "serve":
        names += ("TECHNICAL_CLIENT_SECRET",)
    if mode != "trigger":
        names += ("KEYCLOAK_ADMIN_USERNAME", "KEYCLOAK_ADMIN_PASSWORD")
    config = {name: required(env, name) for name in names}
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


def read_snapshot(http, config, technical_token, revision=False):
    # For a callback, CTS independently verifies the presented access token's
    # signature and authority. Decoded matching claims alone never grant access.
    return http.request("GET", config["CONSULTING_TYPE_SERVICE_URL"] + "/settingsadmin/smtp-credentials",
                        headers={"Authorization": "Bearer " + technical_token, "tenantId": "0",
                                 "Cache-Control": "no-store"}, revision=revision,
                        authenticate_source=revision)


def apply_snapshot(http, config, snapshot):
    transport = smtp_transport(snapshot)
    admin_token = http.login(
        config["KEYCLOAK_URL"], "master", "admin-cli", config["KEYCLOAK_ADMIN_USERNAME"],
        config["KEYCLOAK_ADMIN_PASSWORD"], "KEYCLOAK_UPDATE_FAILED",
    )
    http.request("PUT", config["KEYCLOAK_URL"] + "/admin/realms/" + quote(config["KEYCLOAK_REALM"], safe=""),
                 json.dumps({"smtpServer": transport}).encode(),
                 {"Authorization": "Bearer " + admin_token, "Content-Type": "application/json"},
                 "KEYCLOAK_UPDATE_FAILED")
    return "APPLIED" if transport else "DISABLED_OR_INCOMPLETE"


def reconcile(env):
    config = configuration(env)
    http = HttpClient()
    status = apply_snapshot(http, config, read_snapshot(http, config, technical_login(http, config)))
    if status == "APPLIED":
        print("SMTP_RECONCILE_APPLIED: current Admin Settings")
    else:
        print("SMTP_DISABLED_OR_INCOMPLETE: Keycloak mail disabled until Admin Settings are complete")


def saved_revision(value):
    if (not isinstance(value, str) or not value.isascii() or not value.isdecimal()
            or len(value) > 19 or int(value) > 2**63 - 1):
        raise ReconcileError("SMTP_RECONCILE_SOURCE_REVISION_INVALID")
    return int(value)


def trigger(env, ready_seconds=TRIGGER_READY_SECONDS, retry_seconds=TRIGGER_RETRY_SECONDS):
    config = configuration(env, "trigger")
    url = base_url(required(env, "SMTP_RECONCILE_URL"), "keycloak-reconcile-smtp", config["POD_NAMESPACE"])
    http = HttpClient()
    headers = {"Authorization": "Bearer " + technical_login(http, config),
               "Content-Type": "application/json"}
    deadline = time.monotonic() + ready_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TriggerNotReady()
        try:
            result = http.request("POST", url, b'{"revision":0}', headers, "TRIGGER_FAILED",
                                  timeout=min(40, remaining), trigger_request=True)
            break
        except TriggerNotReady:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(retry_seconds, remaining))
    if (not result or not isinstance(result.get("appliedRevision"), int)
            or isinstance(result["appliedRevision"], bool) or not 0 <= result["appliedRevision"] <= 2**63 - 1
            or result.get("status") not in ("APPLIED", "DISABLED_OR_INCOMPLETE")):
        raise ReconcileError("SMTP_RECONCILE_TRIGGER_FAILED")
    code = "SMTP_RECONCILE_APPLIED" if result["status"] == "APPLIED" else "SMTP_DISABLED_OR_INCOMPLETE"
    print(code + ": installation snapshot acknowledged")


def serve(env):
    config = configuration(env, "serve")
    lock = threading.Lock()
    stopping = threading.Event()
    applied = {"revision": None, "status": None}

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *_):
            pass  # Requests and exception text may contain bearer credentials.

        def reply(self, status, payload):
            code = payload.get("code") if isinstance(payload, dict) else None
            if status >= 400 and code:
                # Codes only. Request lines, headers and response bodies may
                # carry bearer tokens or the SMTP password.
                print("%s %s" % (status, code), file=sys.stderr, flush=True)
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.reply(200 if self.path == "/health" and not stopping.is_set() else 404, {})

        def do_POST(self):
            if self.path != "/smtp/reconcile":
                return self.reply(404, {"code": "SMTP_RECONCILE_ROUTE_NOT_FOUND"})
            if stopping.is_set():
                return self.reply(503, {"code": "SMTP_RECONCILE_STOPPING"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 256 or self.headers.get("Content-Type") != "application/json":
                    raise ValueError()
                payload = json.loads(self.rfile.read(length))
                revision = payload.get("revision") if isinstance(payload, dict) else None
                if (set(payload) != {"revision"} or not isinstance(revision, int)
                        or isinstance(revision, bool) or not 0 <= revision <= 2**63 - 1):
                    raise ValueError()
            except (ValueError, TypeError, UnicodeError, OSError):
                return self.reply(400, {"code": "SMTP_RECONCILE_REQUEST_INVALID"})
            authorization = self.headers.get("Authorization", "")
            if not authorization.startswith("Bearer ") or len(authorization) > 8192:
                return self.reply(403, {"code": "SMTP_RECONCILE_UNAUTHORIZED"})
            token = authorization[7:]
            try:
                verify_technical_identity(token, config["TECHNICAL_SERVICE_SUBJECT"], config["TECHNICAL_CLIENT_ID"])
            except ReconcileError:
                return self.reply(403, {"code": "SMTP_RECONCILE_UNAUTHORIZED"})
            if not lock.acquire(blocking=False):
                return self.reply(503, {"code": "SMTP_RECONCILE_BUSY"})
            try:
                http = HttpClient()
                snapshot, raw_revision = read_snapshot(http, config, token, revision=True)
                current = saved_revision(raw_revision)
                if revision > current or (applied["revision"] is not None and current < applied["revision"]):
                    raise ReconcileError("SMTP_RECONCILE_REVISION_CONFLICT", 409)
                if current != applied["revision"]:
                    status = apply_snapshot(http, config, snapshot)
                    applied.update(revision=current, status=status)
                self.reply(200, {"appliedRevision": current, "status": applied["status"]})
            except ReconcileError as error:
                self.reply(error.status, {"code": str(error)})
            except Exception:
                self.reply(502, {"code": "SMTP_RECONCILE_FAILED"})
            finally:
                lock.release()

    class Server(ThreadingHTTPServer):
        daemon_threads = False
        block_on_close = True

        def server_bind(self):
            # This API has no hostname-dependent response. Avoid an unbounded
            # reverse lookup of the bind-all address during process startup.
            socketserver.TCPServer.server_bind(self)
            self.server_name = "keycloak-reconcile-smtp"
            self.server_port = self.server_address[1]

        def handle_error(self, *_):
            pass  # Never log traceback/header/HTTP body on a failed connection.

    try:
        port = int(env.get("SMTP_RECONCILE_PORT", "8080"))
        if not 0 < port <= 65535:
            raise ValueError()
    except ValueError:
        raise ReconcileError("SMTP_RECONCILE_CONFIGURATION_INVALID: listen port") from None
    server = Server(("0.0.0.0", port), Handler)
    server.timeout = 0.2
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    try:
        while not stopping.is_set():
            server.handle_request()
    finally:
        # Stop accepting before waiting for bounded inflight writes. Recreate
        # must not replace this process until the drain and termination finish.
        server.server_close()


def main():
    try:
        if sys.argv[1:] == ["--serve"]:
            serve(os.environ)
        elif sys.argv[1:] == ["--trigger"]:
            trigger(os.environ)
        elif not sys.argv[1:]:
            reconcile(os.environ)
        else:
            raise ReconcileError("SMTP_RECONCILE_CONFIGURATION_INVALID: mode")
    except ReconcileError as error:
        print(str(error), file=sys.stderr)
        return 2
    except Exception:
        # Do not leak a traceback, an HTTP reply or a credential on an unplanned
        # failure. The pending CTS revision stays eligible for targeted retry.
        print("SMTP_RECONCILE_FAILED", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
