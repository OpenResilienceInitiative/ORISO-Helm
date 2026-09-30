#!/usr/bin/env python3
"""Apply the installed mail-theme locales to an existing Keycloak realm.

The image check runs in the preceding init container. This process holds the
realm-admin password and token only in memory and never prints HTTP payloads.
"""

import json
import os
import sys
import time
from urllib.parse import quote

from keycloak_reconcile_smtp import (
    HttpClient,
    MAX_REALM_RESPONSE_BYTES,
    ReconcileError,
    TriggerNotReady,
    base_url,
)


EXPECTED = {
    "internationalizationEnabled": True,
    "supportedLocales": ["de", "en", "fr", "ru", "ti", "tr"],
    "defaultLocale": "de",
    "emailTheme": "oriso",
}
STARTUP_READY_SECONDS = 180  # Under the 240-second Helm Job deadline.
STARTUP_RETRY_SECONDS = 5


class LocaleError(Exception):
    """A fixed message safe for the Job log."""


def required(env, name):
    value = env.get(name)
    if not isinstance(value, str) or not value.strip():
        raise LocaleError("MAIL_LOCALE_CONFIGURATION_MISSING: " + name)
    return value


def reconcile(env):
    namespace = required(env, "POD_NAMESPACE")
    try:
        url = base_url(required(env, "KEYCLOAK_URL"), "keycloak", namespace)
    except ReconcileError:
        raise LocaleError("MAIL_LOCALE_KEYCLOAK_URL_INVALID") from None
    realm = required(env, "KEYCLOAK_REALM")
    username = required(env, "KEYCLOAK_ADMIN_USERNAME")
    password = required(env, "KEYCLOAK_ADMIN_PASSWORD")
    client = HttpClient()
    ready_by = time.monotonic() + STARTUP_READY_SECONDS
    while True:
        try:
            token = client.login(url, "master", "admin-cli", username, password,
                                 "AUTH_FAILED", transient_startup=True)
            break
        except TriggerNotReady:
            if time.monotonic() + STARTUP_RETRY_SECONDS >= ready_by:
                raise LocaleError("MAIL_LOCALE_KEYCLOAK_NOT_READY") from None
            time.sleep(STARTUP_RETRY_SECONDS)
        except ReconcileError:
            raise LocaleError("MAIL_LOCALE_AUTH_FAILED") from None
    realm_url = url + "/admin/realms/" + quote(realm, safe="")
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    try:
        client.request("PUT", realm_url, json.dumps(EXPECTED).encode(), headers,
                       "REALM_UPDATE_FAILED")
    except ReconcileError:
        raise LocaleError("MAIL_LOCALE_REALM_UPDATE_FAILED") from None
    try:
        actual = client.request("GET", realm_url,
                                headers={"Authorization": "Bearer " + token},
                                failure="REALM_READBACK_FAILED",
                                max_response_bytes=MAX_REALM_RESPONSE_BYTES)
    except ReconcileError:
        raise LocaleError("MAIL_LOCALE_REALM_READBACK_FAILED") from None
    if any(actual.get(key) != value for key, value in EXPECTED.items()):
        raise LocaleError("MAIL_LOCALE_REALM_READBACK_MISMATCH")


def main():
    try:
        reconcile(os.environ)
    except LocaleError as error:
        print(str(error), file=sys.stderr)
        return 2
    except Exception:
        print("MAIL_LOCALE_RECONCILE_FAILED", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
