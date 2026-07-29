# Password reset & account-invite links: runtime configuration and how to verify it

Self-service password reset fails **closed**. If either base URL is unset,
UserService sends no mail at all and logs a warning at startup. This runbook
lists the keys per environment and the checks that prove the feature is live.

## Required keys

| Key | Dev | Pre-Dev |
|---|---|---|
| `PASSWORD_RESET_FRONTEND_BASE_URL` | `https://dev.oriso.org` | `https://app.oriso-dev.site` |
| `PASSWORD_RESET_ADMIN_FRONTEND_BASE_URL` | `https://dev.oriso.org/admin` | `https://admin.oriso-dev.site/admin` |

UserService appends `/password-reset/confirm?token=…` to each base URL, so the
Admin value must already include the `/admin` prefix the Admin panel is served
under.

## Account-invite links (TEN-INV-U6/U7)

Account-invite mails use their own pair of base URLs. Unlike the
password-reset keys, `InviteAcceptUrlBuilder` appends the **full route**
itself — `/admin/tenant-onboarding/{token}` for tenant-admin invites,
`/account-invite/{token}` for every other role — so both values are bare
origins and the admin one must **not** end in `/admin`:

| Key | Pre-Dev |
|---|---|
| `ACCOUNT_INVITE_APP_FRONTEND_BASE_URL` | `https://app.oriso-dev.site` |
| `ACCOUNT_INVITE_ADMIN_FRONTEND_BASE_URL` | `https://admin.oriso-dev.site` |

When unset, both fall back to the system-notification base URL (default
`https://app.oriso.org`). On Pre-Dev the Admin panel lives on its own host,
so the admin value is **required** — otherwise every tenant-admin invite
links to the App host, which does not serve the onboarding route.

## Applying it per environment

Dev is rendered from `values-dev.yaml` in this chart; Pre-Dev values live in
`values-pre-dev.yaml`. Pre-Dev still runs a release rendered from the archived
ORISO-Kubernetes chart (ORISO-Helm#110) — a `helm upgrade` from this chart
against Pre-Dev is not allowed — so until that migration lands the live
rollout is a scoped ConfigMap patch (mirroring ORISO-Admin#392):

```bash
kubectl -n caritas patch configmap userservice-configmap-env --type merge -p '{"data":{"PASSWORD_RESET_FRONTEND_BASE_URL":"https://app.oriso-dev.site","PASSWORD_RESET_ADMIN_FRONTEND_BASE_URL":"https://admin.oriso-dev.site/admin","ACCOUNT_INVITE_APP_FRONTEND_BASE_URL":"https://app.oriso-dev.site","ACCOUNT_INVITE_ADMIN_FRONTEND_BASE_URL":"https://admin.oriso-dev.site"}}'
kubectl -n caritas rollout restart deployment/oriso-platform-userservice
kubectl -n caritas rollout status  deployment/oriso-platform-userservice
```

The Pre-Dev Deployment must also reference every patched key. A ConfigMap
value that no `env` entry imports has no effect — the same drift class as the
platform-admin OTP policy (ORISO-Helm#128). In this chart the UserService
Deployment imports all five link keys (guarded exactly like their ConfigMap
keys); on the archived Pre-Dev release, verify the pod environment after the
patch (see Verification below) and add the `env` entries if any key is
missing.

## SMTP transport

UserService sends the reset mail itself over SMTP. Until now the maintained
chart rendered no SMTP configuration at all, so a Helm-deployed environment
could never send one. The chart now renders:

| Key | Source | Value |
|---|---|---|
| `SMTP_HOST` | `userService.smtpHost` | `mail.dreambau.com` |
| `SMTP_PORT` | `userService.smtpPort` | `587` |
| `SMTP_SECURE` | `userService.smtpSecure` | `false` (STARTTLS) |
| `SMTP_FROM` | `userService.smtpFrom` | `ORISO Platform <monty.burns@oriso.org>` |
| `SMTP_USER` | `userService.smtpUser` (secret values) | the platform-admin mailbox |
| `SMTP_PASSWORD` | `userService.smtpPassword` (secret values) | its password |

`smtpUser` and `smtpPassword` belong in the persistent secret values, never in
a values file in this repository.

Pre-Dev is not rendered from this chart; its ConfigMap and
`oriso-platform-userservice-secrets` were wired to the same identity by hand on
2026-07-28.

## Global SMTP settings

The reset mail is sent directly over SMTP, not through MailService. UserService
reads host/port/secure/from from the **public** ConsultingTypeService
`/settings` and the username/password from the **authenticated**
`/settingsadmin` endpoint, because the public payload deliberately omits
credentials since the CTS-C01 credential-leak fix
(ORISO-ConsultingTypeService#7). All of the following must be set in the
platform settings, otherwise no mail is sent:

- `globalFeatureSystemNotificationEmailsEnabled` = true
- `globalSmtpEnabled` = true
- `globalSmtpHost`, `globalSmtpPort`, `globalSmtpFrom`
- `globalSmtpUsername`, `globalSmtpPassword`

## Verification

Startup log — both warnings must be **absent**:

```bash
kubectl -n caritas logs deploy/<userservice-deployment> | grep -i "password reset is DISABLED"
```

Effective pod environment — all link keys must be present:

```bash
kubectl -n caritas exec deploy/<userservice-deployment> -- env | grep -E 'PASSWORD_RESET|ACCOUNT_INVITE'
```

End-to-end, without any browser: request a reset for an account whose email is
a readable test mailbox, then read the mail through the Test Access Hub.

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  https://api.oriso-dev.site/service/users/password-reset/request \
  -H 'Content-Type: application/json' \
  -d '{"username":"<account>","locale":"de"}'
# expected: 204 for both known and unknown accounts (no account enumeration)

test-access mail mailbox:<simpson>@oriso.org
```

Use an `@oriso.org` mailbox. Every other pool domain is S/MIME encrypted at
rest, so the subject is visible but the body — and therefore the reset link —
is not readable.
