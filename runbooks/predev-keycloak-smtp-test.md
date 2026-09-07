# Temporary PreDev Keycloak SMTP configuration

This one-shot operator helper copies the current platform SMTP configuration from
ConsultingTypeService into the initially empty `online-beratung` realm for a
coordinated security-email test. It does not send mail, change reset permission,
change the email theme, or run a synchronization job.

## Source, authority and transport

`GET https://predev.oriso.org/service/settings` supplies SMTP enabled, host, port,
from and secure fields. `GET /service/settingsadmin/smtp-credentials` supplies the
existing username and decrypted password. CTS `ApplicationSettingsController`
guards credentials with `AuthorisationService.isSuperAdmin()` (token tenant ID
zero), and `ApplicationSettingsServiceFacade` uses its existing encryption service.
No database access or encryption-key export is needed. Security-email transport is
not gated on the operational notification preference.

The destination is only `https://predev.oriso.org`, realm `online-beratung`. The
Keycloak admin token needs realm read and realm update authority. The coordinator
supplies both short-lived tokens as JSON on stdin, through its existing in-memory
authentication mechanism. Never put tokens in CLI arguments, shell history,
committed files, or diagnostic output. The helper does not acquire tokens itself.

Auth input fields are `platformToken` and `keycloakToken`. Restore/check need only
`keycloakToken`. The default Keycloak path prefix is `/auth`; an explicitly
verified root deployment can use `--keycloak-prefix ''`. Redirects are refused.

**TLS boundary:** inspected Keycloak 26.6.3 `DefaultEmailSenderProvider` from the
local Maven artifact with `javap -c -p`: `buildEmailProperties` maps `starttls` to
`mail.smtp.starttls.enable` only, and does not forward arbitrary SMTP map keys or
set `mail.smtp.starttls.required`. UserService requires STARTTLS when secure=false.
Consequently this helper refuses secure=false; it supports only the exact existing
implicit-TLS source (`globalSmtpSecure=true`) and does not guess a new port or
weaken TLS. If the source requires STARTTLS, stop and report this configuration
limitation. There is no realm-only required-STARTTLS switch in the inspected provider.

UserService can prefer its SMTP_USER/SMTP_PASSWORD environment over CTS credentials.
This helper explicitly copies the CTS platform source; if claiming parity with
UserService's current effective credentials, the coordinator must compare those
in memory separately without logging them.

## Coordinated apply, check, restore

The coordinator pipes auth JSON directly to each command below. A marker must not
already exist on apply; it must be in a private operator artifact directory. The
marker contains only target, empty original SMTP map and nonsecret candidate
host/port/from/TLS flags. Username, password, tokens and secret hashes are excluded.

```sh
python3 scripts/predev-keycloak-smtp.py apply \
  --origin https://predev.oriso.org --realm online-beratung \
  --marker /absolute/operator-artifacts/keycloak-smtp-empty-original.json

python3 scripts/predev-keycloak-smtp.py check \
  --origin https://predev.oriso.org --realm online-beratung \
  --marker /absolute/operator-artifacts/keycloak-smtp-empty-original.json

python3 scripts/predev-keycloak-smtp.py restore \
  --origin https://predev.oriso.org --realm online-beratung \
  --marker /absolute/operator-artifacts/keycloak-smtp-empty-original.json
```

Apply verifies empty SMTP twice, writes the marker exclusively before PUT, sends
only `{smtpServer: candidate}`, and checks sanitized readback. An interrupted or
uncertain response leaves the marker for explicit recovery. Do not blindly rerun
apply. The coordinator must run check and restore in its cleanup path after the
bounded real-mail test, including test failure. This command is deliberately not
a long-running automatic restore/sync process.

Restore verifies the marker target and exact nonsecret candidate shape and expected
key set before sending only `{smtpServer: {}}`. An already empty realm is a
successful idempotent restore. A different visible configuration is refused.
**Limitation:** Keycloak masks passwords, so password-only concurrent changes
cannot be detected without storing secret material; username changes are also
not compared because the marker omits credentials. The API has no compare-and-swap
precondition here: a change between readback and PUT remains possible. Coordinate
exclusive configuration ownership for the short test window. Retain the marker
and sanitized success states as evidence; never claim restored without readback.

## Local verification

`python3 scripts/test_predev_keycloak_smtp.py` uses mocked HTTP only and checks
source mapping, no notification-preference gate, bad target, existing state and
marker guards, concurrent visible changes, unavailable/ciphertext credentials,
strict TLS refusal, restore idempotence and secret-free error/marker output.
No real realm request or email send is part of these tests.
