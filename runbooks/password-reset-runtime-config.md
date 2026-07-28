# Password reset: runtime configuration and how to verify it

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

Dev is rendered from `values-dev.yaml` in this chart. Pre-Dev still runs a
release rendered from the archived ORISO-Kubernetes chart, so until that
migration lands it needs a scoped ConfigMap patch instead:

```bash
kubectl -n caritas patch configmap userservice-configmap-env --type merge -p '{"data":{"PASSWORD_RESET_FRONTEND_BASE_URL":"https://app.oriso-dev.site","PASSWORD_RESET_ADMIN_FRONTEND_BASE_URL":"https://admin.oriso-dev.site/admin"}}'
kubectl -n caritas rollout restart deployment/oriso-platform-userservice
kubectl -n caritas rollout status  deployment/oriso-platform-userservice
```

The Pre-Dev Deployment must also reference both keys. A ConfigMap value that no
`env` entry imports has no effect — the same drift class as the platform-admin
OTP policy (ORISO-Helm#128).

## SMTP transport

UserService sends the reset mail itself over SMTP. Until now the maintained
chart rendered no SMTP configuration at all, so a Helm-deployed environment
could never send one. The chart now renders:

| Key | Source | Value |
|---|---|---|
| `SMTP_HOST` | `userService.smtpHost` | `mail.dreambau.com` |
| `SMTP_PORT` | `userService.smtpPort` | `587` |
| `SMTP_SECURE` | `userService.smtpSecure` | `false` (STARTTLS) |
| `SMTP_FROM` | `userService.smtpFrom` | `ORISO Platform <herb.powell@oriso.org>` |
| `SMTP_USER` | `userService.smtpUser` (secret values) | the platform-admin mailbox |
| `SMTP_PASSWORD` | `userService.smtpPassword` (secret values) | its password |

`smtpUser` and `smtpPassword` belong in the persistent secret values, never in
a values file in this repository.

Pre-Dev is not rendered from this chart; its ConfigMap and
`oriso-platform-userservice-secrets` were wired to the same identity by hand on
2026-07-28.

## Global SMTP settings

The reset mail is sent directly over SMTP, not through MailService.
UserService reads the transport settings from the **public**
ConsultingTypeService `/settings`, so these must be set in the platform
settings or no mail is sent:

- `globalFeatureSystemNotificationEmailsEnabled` = true
- `globalSmtpEnabled` = true
- `globalSmtpHost`, `globalSmtpPort`, `globalSmtpFrom`

The username and password come from `SMTP_USER` / `SMTP_PASSWORD` above, not
from the platform settings. The public payload has deliberately omitted them
since the CTS-C01 credential-leak fix (ORISO-ConsultingTypeService#7), and the
authenticated `/settingsadmin/smtp-credentials` endpoint requires a token with
`tenantId = 0` — which an unauthenticated, asynchronously dispatched password
reset never has. Keeping the credentials in the deployment secret is the only
source that works for that flow.

Per-tenant SMTP is separate: a Träger's own settings live in
`tenantservice.tenant.settings.smtp` and are used for tenant-scoped
notification mail. The public tenant endpoint strips `username` and `password`
from that block, so configuring it does not expose the mailbox.

## Verification

Startup log — both warnings must be **absent**:

```bash
kubectl -n caritas logs deploy/<userservice-deployment> | grep -i "password reset is DISABLED"
```

Effective pod environment — both keys must be present:

```bash
kubectl -n caritas exec deploy/<userservice-deployment> -- env | grep PASSWORD_RESET
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
