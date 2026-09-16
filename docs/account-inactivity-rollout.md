# Account inactivity access gate

Status: chart wiring and local render verification only. No rollout or live session
revocation is claimed. Tracks [Helm #352](https://github.com/OpenResilienceInitiative/ORISO-Helm/issues/352)
and [UserService #1172](https://github.com/OpenResilienceInitiative/ORISO-UserService/issues/1172).

## Values and rollout order

| Value | Default | Effect |
| --- | --- | --- |
| `global.accountInactivity.accessGateEnabled` | `false` | Checks ORISO API requests through UserService before proxying them. |
| `userService.accountInactivity.enabled` | `false` | Enables the daily UserService lifecycle scheduler. |
| `userService.accountInactivity.dryRun` | `true` | Keeps the enabled scheduler in candidate-report mode. |

Changing either scheduler flag changes a scoped pod-template checksum so UserService
rolls out and consumes the new environment values.

The last two values populate `ACCOUNT_INACTIVITY_ENABLED` and
`ACCOUNT_INACTIVITY_DRY_RUN`. UserService owns the daily UTC cron default.

1. Deploy the reviewed UserService version with the database migrations and the
   `/users/account-inactivity/access` endpoint. Keep execution disabled.
2. Check the candidate report and migration baseline. If scheduling reports is
   useful, set `enabled: true`, leaving `dryRun: true`.
3. Enable the ingress gate. Validate anonymous public requests, active JWTs,
   rejected suspended JWTs, tenant headers and all rewritten API paths. The
   internal URL must resolve from the ingress controller to the release namespace.
4. Only after the reviewed report and runtime checks, explicitly set
   `enabled: true` and `dryRun: false`. Record the deployed image/configuration,
   evidence and operator decision on the parent issue.

Disabling execution does not clear existing suspension records. Do not turn off
an already active access gate as a substitute for authorized reactivation: doing
so removes the cross-service check for already issued JWTs. UserService database
and API availability are prerequisites while the gate is enabled.

## Request and route contract

The gate calls
`http://userservice.<namespace>.svc.cluster.local:8080/users/account-inactivity/access`
with GET. It contacts the Kubernetes service directly, avoiding ingress recursion
and the external `/service` rewrite. The UserService endpoint accepts anonymous
requests without granting downstream authorization, and validates authenticated
requests against the current lifecycle state. A 401/403 denies the original
request. An unavailable or failing gate fails closed; it must not fall back to
proxying the protected API request.

Authorization, Cookie, X-Tenant-Id and tenantId are forwarded. Forwarding a cookie
is not a new cookie-to-JWT authentication mechanism: the identity formats accepted
by UserService remain authoritative. No auth-cache key is configured, so an old
successful check is not cached across a later suspension.

The shared helper covers UserService, TenantService, AgencyService and
ConsultingTypeService API routes, including regex rewrites, numeric tenant IDs,
public tenant API aliases, tenant-admin roots/subpaths, the agency internal alias,
and the UserService Matrix compatibility API. UserService's `/service/matrix` and
`/sessions/.../service/matrix` accept the ORISO identity; they are distinct from
Synapse's Matrix-token API.

When enabled, the 14 main API paths move to `account-inactivity-api-ingress`.
Their destinations, ports, path types, host, TLS and existing annotations remain
unchanged. With the gate off, those paths stay on `main-ingress`. Specialized
API ingresses keep their existing rewrites and CORS annotations.

Keycloak `/auth`, frontend/admin/static pages, public editor `/media` images,
Synapse `/_matrix` and `/_synapse`, Element Call and LiveKit JWT/SFU routes are
excluded. Operational health/SigNoz dashboards also keep their own authentication.
Matrix/LiveKit have their own credentials and suspension controls.

## Open connections and evidence boundary

Ingress external auth checks a request before forwarding it. It does not terminate
an already upgraded WebSocket, an established media session or an in-flight long
request. The chart's explicit real-time routes are LiveKit SFU and the separate
Matrix/Element Call routes; they remain under their own protocols. Any API
WebSocket handshake is checked if its path is covered, but subsequent frames do
not trigger another auth subrequest. Matrix account locking and live session
termination must be verified independently before claiming full suspension.

Local checks:

```sh
python3 tests/render_account_inactivity_gate_test.py
helm lint . -f values.yaml.default -f secrets.yaml.default \
  --set tenantService.smtpPasswordEncryptionSecret=render-test-secret \
  --set consultingTypeService.smtpPasswordEncryptionSecret=render-test-secret \
  --set global.secrets.redisdefaultPass=render-test-secret
```

The render suite checks all 45 baseline routes, gate coverage/exclusions, original
rewrite/destination preservation, identity headers, no auth-result caching and
scheduler environment defaults/overrides. It does not prove a running ingress
controller's generated configuration or live credential revocation.

The annotation contract follows the ingress-nginx
[external authentication documentation](https://kubernetes.github.io/ingress-nginx/user-guide/nginx-configuration/annotations/#external-authentication).
