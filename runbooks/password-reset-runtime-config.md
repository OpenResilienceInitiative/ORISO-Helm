# Password reset & account-invite links: runtime configuration and how to verify it

The chart always renders these keys (ORISO-Helm#366). An explicit
`userService.*BaseUrl` value wins and is validated; an empty one derives from
`global.domainName` (`<scheme>://<domainName>`, admin reset
`<scheme>://<domainName>/admin`; the scheme is `https`, or `http` when
`global.enableTls` is `false`). An empty or placeholder `global.domainName`
fails `helm template`/`helm install`. This runbook lists the keys per
environment and the checks that prove the feature is live.

## Required keys

| Key | Dev | Pre-Dev |
|---|---|---|
| `PASSWORD_RESET_FRONTEND_BASE_URL` | `https://dev.oriso.org` | `https://predev.oriso.org` |
| `PASSWORD_RESET_ADMIN_FRONTEND_BASE_URL` | `https://dev.oriso.org/admin` | `https://predev.oriso.org/admin` |

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
| `ACCOUNT_INVITE_APP_FRONTEND_BASE_URL` | `https://predev.oriso.org` |
| `ACCOUNT_INVITE_ADMIN_FRONTEND_BASE_URL` | `https://predev.oriso.org` |

When unset, both derive from `global.domainName` (`<scheme>://<domainName>`,
scheme per `global.enableTls`). Dev and Pre-Dev serve the Admin panel under
`/admin` on the App host, so the derived values are correct there. Set the
admin value explicitly only where the Admin panel has its own host.

## Applying it per environment

Dev is rendered from `values-dev.yaml` in this chart; Pre-Dev values live in
`values-pre-dev.yaml`. Pre-Dev still runs a release rendered from the archived
ORISO-Kubernetes chart (ORISO-Helm#110) — a `helm upgrade` from this chart
against Pre-Dev is not allowed — so until that migration lands the live
rollout is a scoped ConfigMap patch (mirroring ORISO-Admin#392):

```bash
kubectl -n caritas patch configmap userservice-configmap-env --type merge -p '{"data":{"PASSWORD_RESET_FRONTEND_BASE_URL":"https://predev.oriso.org","PASSWORD_RESET_ADMIN_FRONTEND_BASE_URL":"https://predev.oriso.org/admin","ACCOUNT_INVITE_APP_FRONTEND_BASE_URL":"https://predev.oriso.org","ACCOUNT_INVITE_ADMIN_FRONTEND_BASE_URL":"https://predev.oriso.org"}}'
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

## Platform SMTP setup

The chart validates public link origins before installation but does not
configure an SMTP provider. Its install notes tell a new operator to sign in
as the first platform admin and enter the platform mail settings in Admin
Settings. UserService reads one coherent settings snapshot through its
technical identity; Helm neither exports `SMTP_*` values to UserService nor
supplies a fallback server. The first-admin password and authenticator path
appears mail-independent in source inspection. A fresh-install walkthrough is
still required before ruling out a one-time bootstrap definitively.

The following Admin Settings values are needed before mail-dependent actions
can deliver:

- `globalFeatureSystemNotificationEmailsEnabled` = true
- `globalSmtpEnabled` = true
- `globalSmtpHost`, `globalSmtpPort`, `globalSmtpFrom`
- `globalSmtpUsername`, `globalSmtpPassword`

Missing or incomplete saved settings produce a named configuration result in
the Admin SMTP diagnostic and mail send paths; an unavailable Admin Settings
service or technical identity is reported separately. Password-reset request
HTTP 204 protects account existence and is not proof of mail delivery. Check a
received test mail before treating setup as complete. The provider-specific
host, sender and credentials are entered only in Admin Settings.

The existing Dev and Pre-Dev overlay `userService.smtp*` fields record an
earlier incident workaround. This chart no longer reads or exports those
fields. Deploy the Admin-managed UserService source before applying this chart
change to an environment still running the deployment-owned SMTP provider.

## Verification

Before install, verify that the chart renders the expected public URLs and
that the install notes explain the Admin Settings SMTP step. Use only a
non-secret environment values file and synthetic render credentials for this
terminal check. Helm's `--hide-secret` hides `Secret` objects, but it does not
redact values repeated in other rendered manifests; **never pass the real
environment secrets file to a dry run whose output is displayed or shared**.
The Redis example of this existing chart limitation is tracked in
[ORISO-Helm #192](https://github.com/OpenResilienceInitiative/ORISO-Helm/issues/192).
A successful render does not establish that SMTP is configured:

```bash
helm install oriso . --dry-run=client --hide-secret \
  -f values.yaml.default -f <environment-values.yaml> \
  -f secrets.yaml.default -f tests/fixtures/render-required-secrets.yaml \
  --set-string global.secrets.redisdefaultPass=render-only-canary \
  --set-string tenantService.smtpPasswordEncryptionSecret=render-only-canary \
  --set-string consultingTypeService.smtpPasswordEncryptionSecret=render-only-canary
```

After rollout, inspect only the UserService Deployment's declared environment
variable names. All public link keys must be present, and there must be no
deployment-owned `SMTP_*` transport keys. Do not print environment values or
Secret data while checking this:

```bash
kubectl -n caritas get deployment <userservice-deployment> \
  -o jsonpath='{range .spec.template.spec.containers[*].env[*]}{.name}{"\n"}{end}' \
  | grep -E 'PASSWORD_RESET|ACCOUNT_INVITE|SMTP_'
```

End-to-end, without any browser: request a reset for an account whose email is
a readable test mailbox, then read the mail through the Test Access Hub.

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  https://predev.oriso.org/service/users/password-reset/request \
  -H 'Content-Type: application/json' \
  -d '{"username":"<account>","locale":"de"}'
# expected: 204 for both known and unknown accounts (no account enumeration)

test-access mail mailbox:<simpson>@oriso.org
```

Use an `@oriso.org` mailbox. Every other pool domain is S/MIME encrypted at
rest, so the subject is visible but the body — and therefore the reset link —
is not readable.
