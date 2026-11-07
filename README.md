# ORISO Helm Chart

Helm chart for deploying the [ORISO](https://github.com/OpenResilienceInitiative) online counseling platform on Kubernetes.

The chart covers the full stack: Keycloak, MariaDB, MongoDB, RabbitMQ, Redis, Matrix/Synapse, LiveKit, and all backend/frontend services.

## Getting started

Configuration is split across two files — both are gitignored and must be created locally before deploying.

### 1. Set up your config

Copy the values template and fill in your domain and realm:

```bash
cp values.yaml.default values.yaml
```

Open `values.yaml` and update:

- `global.domainName` — **required**, your public host (no scheme, no path). Every
  public URL and mail link derives from it; empty or a placeholder fails the install
- `global.keycloak.realm` — your Keycloak realm name (appears in several URL fields)
- `matrix.synapseServerName` / `matrixServerName` — your Matrix server name

### 2. Set up your secrets

Copy the secrets template and fill in all credentials:

```bash
cp secrets.yaml.default secrets.yaml
```

Open `secrets.yaml` and replace every `changeme` with a real value. Fields to fill in:

- `global.secrets.*Password` / `*Pass` — database and service passwords
- `global.secrets.matrixRegistrationSharedSecret` — Matrix shared secret
- `global.keycloak.technicalUser.password` — Keycloak technical user password
- `global.keycloak.serviceTechUserId` (in `values.yaml.default`, not a secret) — Keycloak user id (`sub`) of the `technical` user, rendered as TenantService `TECHNICAL_SERVICE_SUBJECT`. **Required, a UUID, no default.** On an existing realm look up the live id once (admin console, Users > technical > ID). Only a fresh install that imports `realm.json` may use its fixed id `8294c392-e1e0-405b-ac2f-ba3043cbad3e`. The reconcile hook compares it with the realm before changing anything and fails the release on a mismatch.
- `postgres.postgresPassword` — PostgreSQL password, rendered into `matrix-postgres-secret`, not a ConfigMap. The default engine is `sqlite3` to preserve compatibility with existing deployments. For a fresh Matrix database during a planned reset, explicitly set `matrix.database.engine: postgres` in your own `values.yaml`. Existing PostgreSQL installations must retain that override on every upgrade. Empty PostgreSQL storage initializes with UTF-8 and C locale. Follow the [fresh-install or migration instructions](runbooks/matrix-sqlite-to-postgres.md); selecting PostgreSQL neither migrates Matrix data nor deletes application/Keycloak users.
- `global.matrix.matrixAdminUsername` / `matrixAdminPassword` — Matrix admin credentials (must live under `global:` so subcharts can read them)
- `online-counseling-mongodb.*Password` / `*Pass` — MongoDB passwords
- `online-counseling-mariadb.dbRootPassword` — MariaDB root password
- `matrixrtcAuth.membershipReaderPassword` — password for the non-admin MatrixRTC membership reader user that Helm bootstraps in Synapse
- `livekit.api.key` / `livekit.api.secret` — LiveKit API credentials
- `tenantService.springDatasourcePassword` / `springRabbitmqPassword`
- `agencyService.serviceEncryptionAppkey` — AgencyService encryption key (Matrix service-account passwords). **Required** — the chart refuses to render if it is blank, because an empty key silently breaks agency creation. Rotating it invalidates already-stored credentials.
- `userService.serviceEncryptionAppkey` / `identityTechnicalUser*` — `identityTechnicalUser*` is the UserService's service identity (realm user `technical`, realm role `technical` only). **Required.** The former duplicate pair `userService.keycloakTechnical*` is no longer read.
- `global.secrets.keycloakServiceAdminUsername` / `keycloakServiceAdminPassword` — backend Keycloak admin identity (`svc-keycloak-admin`: `manage-users`, `view-users`, `query-users`, `view-realm`, `otp-config-admin`). **Required**; the password may not be empty or `changeme`, and the username may not be `technical` or the master bootstrap admin name `keycloakAdminUsername` (the hook fails). Mounted into the UserService as `KEYCLOAK_CONFIG_ADMIN_*`; the master bootstrap admin credentials (`keycloakAdmin*`, next item) stay deployment-time only (Keycloak pod, bootstrap and service-identity hooks; never the SMTP Jobs). The hook `keycloak-reconcile-service-identities` converges both identities to these exact role sets on every install and upgrade (ORISO-Helm#367).
- `global.secrets.keycloakAdminUsername` / `keycloakAdminPassword` — master-realm bootstrap admin, the only break-glass identity (**required**, no chart default: the installer chooses the name, e.g. `bootstrap-admin`; an empty name always fails, and a fresh install refuses `admin`, `realmadmin`, `keycloak`, `root` in any case and the ORISO-realm user name; upgrades keep an existing name). Read by Keycloak only on first start. Use, notice and rotation: `runbooks/keycloak-break-glass.md` (ORISO-Helm#422).

### 3. Install / Upgrade

For the coordinated Matrix-only/Matryoshka cutover, never edit image tags into
`values.yaml`. Frontend, Element Call, UserService, AgencyService, Synapse and
both MatrixRTC authorization images accept only complete
`repository@sha256:<digest>` references.

After the cross-repository release manifest contains reviewed registry
digests, attached security evidence, rotated secrets and the status
`ready-for-predev`, generate and verify the exact Helm overlay:

```bash
./scripts/cutover-release-preflight.py \
  /path/to/ORISO-Matryoshka-Release-Manifest.yaml \
  --output-values /path/to/new-cutover-digests.yaml
```

The command fails closed on `STOP_SHIP` placeholders, zero/wrong digests,
missing PR or security evidence, forbidden legacy render artifacts, or any
rendered image that differs from the manifest. It refuses to overwrite an
existing output file.

Use the verified overlay after the environment values and before secrets:

```bash
helm upgrade --install caritas ./ --namespace caritas --create-namespace \
  --wait-for-jobs --timeout 15m \
  -f values.yaml -f values-dev.yaml \
  -f /path/to/new-cutover-digests.yaml -f secrets.yaml
```

```bash
helm upgrade --install caritas ./ --namespace caritas --create-namespace --wait-for-jobs --timeout 15m \
  -f values.yaml -f values-<env>.yaml -f secrets.yaml
```

`values.yaml` (or the environment overlay `values-<env>.yaml`) must set
`global.domainName`; without it the install fails on purpose.

The first `caritas` is the Helm release name, the second is the Kubernetes namespace. Both can be changed to suit your environment.

### MatrixRTC / LiveKit runtime Secrets

LiveKit and MatrixRTC auth read their sensitive runtime material from
Kubernetes Secrets rendered by Helm from the ignored environment
`secrets.yaml`. Set these values before installing:

- `matrixrtcAuth.membershipReaderPassword` — password for
  `matrixrtcAuth.membershipReaderUserId`; Helm registers/logs in this Matrix
  user and patches the generated access token into `matrixrtc-auth-secrets`
- `livekit.api.key` / `livekit.api.secret` — shared LiveKit API credentials
- `matrixrtcAuth.redisUrl` — optional external Redis URL; leave blank to use
  the in-chart Redis service and `global.secrets.redisdefaultPass`
- `matrixrtcAuth.membershipToken` — optional manual Matrix access token
  override; leave blank to use the automatic bootstrap job
- `matrixrtcAuth.callPolicyToken` — dedicated high-entropy secret shared only
  by the MatrixRTC policy gateway and UserService; this authenticates the
  cluster-internal fresh tenant-policy lookup

During `helm upgrade --install`, the chart creates `matrixrtc-auth-secrets` and
`livekit-config`, then a `matrixrtc-bootstrap-token` Job waits for Synapse,
creates or reuses the membership reader user, logs in, and patches the real
Matrix access token into `matrixrtc-auth-secrets`. MatrixRTC auth waits for
that token before starting, so LiveKit, MatrixRTC auth, and Element Call can
start without a second bootstrap step.

The gateway resolves every new call/reconnect against UserService before a
LiveKit grant is issued. A tenant permission change therefore affects already
open browser tabs on their next call, reconnect, or rejoin. Policy lookup is
fail-closed; UserService unavailability never falls back to a permissive grant.

### Environment overlays (dev vs prod)

`values.yaml.default` is a **prod-safe baseline** (`springProfilesActive: prod`,
no dummy-data seeding, OTP off). Layer an environment overlay on top instead of
maintaining separate copies:

```bash
# development: seeds dummy data, dev Spring profile, fast test-user login
helm upgrade --install caritas ./ -n caritas --create-namespace \
  -f values.yaml -f values-dev.yaml -f secrets.yaml

# production (what the hoster runs via ArgoCD)
helm upgrade --install caritas ./ -n caritas --create-namespace \
  -f values.yaml -f values-prod.yaml -f secrets.yaml
```

Overlays only change *test friction* and per-environment wiring. **Encryption is
never toggled** — there is no dev "encryption off" mode by design (see
`docs/infrastructure-report-2026-07.md` §7).

### SigNoz by environment

With `values-dev.yaml`, and `values-pre-dev.yaml` layered on top for Pre-Dev,
`signoz.enabled=true` deploys SigNoz, enables application OTLP export by default,
and serves the UI at `https://<global.domainName>/signoz/`. Set
`signoz.orisoIngress.enabled=false` to suppress the ORISO UI route. The
separately configured browser metrics route stays available. For an external
collector in those environments, set
`global.observability.otlpEnabled=true` and provide its HTTP host and port in
`global.observability.otlpCollectorHost`.

Layer `values-prod.yaml` over the base values for production. With that overlay,
`signoz.enabled=true` can prepare the internal SigNoz services while the Java
service OTLP exporters and the ORISO SigNoz ingress default to off. The
historical pseudonymization pipeline in [Helm PR #65](https://github.com/OpenResilienceInitiative/ORISO-Helm/pull/65)
targeted a previous collector template. The current chart needs a verified
privacy pipeline, retention, backup and restore, and authenticated UI access
before production activation. See [E2E issue #99, F.4](https://github.com/OpenResilienceInitiative/ORISO-E2E/issues/99).
