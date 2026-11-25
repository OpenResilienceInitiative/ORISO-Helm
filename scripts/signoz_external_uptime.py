#!/usr/bin/env python3
"""Check the public Dev SigNoz endpoints from an independent runner."""

import argparse
from datetime import datetime, timezone
import http.client
import json
import ssl
import time
from typing import NamedTuple


ALIAS_HOST = "signoz.oriso.org"
CANONICAL_HOST = "dev.oriso.org"
UI_PATH = "/signoz/"
CANONICAL_URL = f"https://{CANONICAL_HOST}{UI_PATH}"
HEALTH_PATH = "/signoz/api/v1/health?live=1"
EXPIRY_WARNING_SECONDS = 21 * 86400
EXPIRY_CRITICAL_SECONDS = 7 * 86400
HEALTH_BODY_LIMIT = 4096
EXIT_CODES = {"OK": 0, "WARNING": 1, "CRITICAL": 2, "UNKNOWN": 3}


class Response(NamedTuple):
    status: int
    location: str | None
    content_type: str
    body: bytes
    expires_at: float | None


def get(host: str, path: str, context: ssl.SSLContext, *, body_limit: int = 0) -> Response:
    connection = http.client.HTTPSConnection(host, context=context, timeout=8)
    try:
        connection.connect()
        peer = connection.sock
        if peer is None:
            raise OSError("TLS socket is unavailable")
        certificate = peer.getpeercert()
        expires_at = ssl.cert_time_to_seconds(certificate["notAfter"]) if certificate else None
        connection.request("GET", path, headers={"Cache-Control": "no-cache"})
        with connection.getresponse() as response:
            content_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
            body = response.read(body_limit + 1) if body_limit and response.status == 200 else b""
            return Response(response.status, response.getheader("Location"), content_type, body, expires_at)
    finally:
        connection.close()


def healthy_response(response: Response) -> bool:
    if response.content_type != "application/json" or len(response.body) > HEALTH_BODY_LIMIT:
        return False
    try:
        payload = json.loads(response.body)
    except (UnicodeError, ValueError, RecursionError):
        return False
    return isinstance(payload, dict) and payload.get("status") == "ok"


def check(context: ssl.SSLContext, *, now: float) -> dict:
    issues = []
    statuses = {}
    expiries = {}
    for name, host, path, limit in (
        ("alias", ALIAS_HOST, "/", 0),
        ("ui", CANONICAL_HOST, UI_PATH, 0),
        ("health", CANONICAL_HOST, HEALTH_PATH, HEALTH_BODY_LIMIT),
    ):
        try:
            response = get(host, path, context, body_limit=limit)
        except ssl.SSLError:
            issues.append(f"{name}_tls")
            continue
        except (OSError, http.client.HTTPException):
            issues.append(f"{name}_connection")
            continue

        statuses[name] = response.status
        certificate_host = "alias" if name == "alias" else "canonical"
        expires_at = response.expires_at
        if expires_at is None or expires_at <= now:
            issues.append(f"{name}_certificate")
        else:
            expiries[certificate_host] = min(expires_at, expiries.get(certificate_host, expires_at))
        if name == "alias":
            if response.status not in {301, 302, 303, 307, 308} or response.location != CANONICAL_URL:
                issues.append("alias_redirect")
        elif response.status != 200:
            issues.append(f"{name}_status")
        elif name == "health" and not healthy_response(response):
            issues.append("health_payload")

    state = "CRITICAL" if issues else "OK"
    certificates = {}
    for name, expires_at in expiries.items():
        seconds_remaining = expires_at - now
        certificates[name] = {
            "not_after": datetime.fromtimestamp(expires_at, timezone.utc).isoformat(),
            "seconds_remaining": int(seconds_remaining),
        }
        if seconds_remaining < EXPIRY_CRITICAL_SECONDS:
            issues.append(f"{name}_certificate_expires_within_7_days")
            state = "CRITICAL"
        elif seconds_remaining < EXPIRY_WARNING_SECONDS:
            issues.append(f"{name}_certificate_expires_within_21_days")
            if state == "OK":
                state = "WARNING"

    return {
        "state": state,
        "issues": issues,
        "http_status": statuses,
        "checked_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "certificates": certificates,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ca-file", help="Path to the runner's trusted CA bundle")
    args = parser.parse_args(argv)
    try:
        context = ssl.create_default_context(cafile=args.ca_file)
    except OSError:
        result = {"state": "UNKNOWN", "issues": ["tls_context"]}
    else:
        result = check(context, now=time.time())
    print(json.dumps(result, sort_keys=True))
    return EXIT_CODES[result["state"]]


if __name__ == "__main__":
    raise SystemExit(main())
