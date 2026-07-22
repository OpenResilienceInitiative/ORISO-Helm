# PreDev platform-admin 2FA runtime gate

Run this gate after every PreDev UserService deployment and before accepting an
Admin release candidate:

```bash
ssh oriso-predev 'bash -s' < scripts/check-predev-platform-admin-2fa.sh
```

The gate fails unless all of the following runtime facts agree:

- `userservice-configmap-env` sets
  `IDENTITY_OTP_ALLOWED_FOR_TENANT_SUPER_ADMINS=true`.
- the UserService Deployment imports that exact ConfigMap key;
- the rollout is ready and the effective pod environment still contains
  `true`; and
- the running UserService image is digest-pinned.

This is an environment wiring check, not a test-only authentication bypass. It
does not disable App-TOTP, accept fixed OTP values, or weaken the production
policy. The companion ORISO-E2E acceptance test signs in through Dreambau Test
Access with the managed platform-admin account and a real App-TOTP code.

The defaults target the current PreDev names. For another namespace or release,
set `NAMESPACE`, `DEPLOYMENT`, or `CONFIGMAP` explicitly. `KUBECTL_BIN` is
injectable for the automated failure-path tests.
