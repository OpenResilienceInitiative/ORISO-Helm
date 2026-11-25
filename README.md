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

Open `secrets.yaml` and replace every `changeme` with a real value. The install
fails while any value still contains `changeme`, and the error names it; the
database, RabbitMQ, Redis, Matrix and Keycloak admin passwords must also be at
least 12 characters (ORISO-Helm#540). Fields to fill in:

- `global.secrets.*Password` / `*Pass` — database and service passwords. Synapse loads its Redis settings, including `global.secrets.redisdefaultPass`, from the mounted credentials Secret.
- `global.secrets.matrixRegistrationSharedSecret` — Matrix shared secret without surrounding whitespace. Synapse reads it from the mounted `matrix-synapse-credentials`. Keep the existing value for the storage migration and coordinate any later rotation with the existing clients.
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
- `global.keycloak.serviceAdminClientId` / `global.secrets.keycloakBackendAdminClientSecret` — the UserService Keycloak admin account (`backend-admin` by default). Its realm role is `otp-config-admin`. The reconciliation hook grants `manage-users`, `view-users`, `query-users`, `view-realm` and `query-clients` in `realm-management`. See the [backend service-client runbook](runbooks/backend-service-clients.md) for preparation, subject pins and rollout checks.
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

### Matrix server discovery rollout

First upgrade with `matrix.routeServerDiscoveryViaSynapse` set to `false`.
After the Synapse pod is Ready, check that `matrix-synapse:8802` serves
`/.well-known/matrix/server` with the public hostname and port 443.
Then enable the setting to move the exact public route to that listener.
The route switch does not restart Synapse. Check the public JSON response and
CORS header after the switch. Client discovery and the existing Matrix API
routes keep their current routing.

Before controller snippets are disabled, setting the option to `false` restores
the legacy Ingress response.

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

### Move ingress header and rate-status snippets to controller settings

Before switching routes, prepare the serving ingress-nginx controller with
`limit-req-status-code: "429"` and add `Permissions-Policy` to its existing
`global-allowed-response-headers` list. The status setting applies to the whole
controller. Preserve its other keys, including the Dev HTTP/2 mitigation and
access-log settings, and verify the reload on every ready controller replica.
For the standalone Dev controller, persist the keys in its own configuration.

Then set `nginxController.useStandardRoutePolicies: true` for the application
release. For bundled ingress-nginx, Helm retains both controller settings after
activation. The application chart still supplies the same header values, rate
thresholds and paths. Confirm the main and Element Call headers on ordinary and
redirect responses, and use the approved rate-limit check to confirm `429`.

The remaining snippets are migrated under
[Helm #536](https://github.com/OpenResilienceInitiative/ORISO-Helm/issues/536).
Until that work makes snippet disabling possible, setting the new option to
`false` restores the legacy per-route annotations.

### SigNoz by environment

With `values-dev.yaml`, `signoz.enabled=true` deploys SigNoz, enables
application OTLP export by default, and serves the UI at
`https://<global.domainName>/signoz/`. For an external collector, set
`global.observability.otlpEnabled=true` and provide its HTTP host and port in
`global.observability.otlpCollectorHost`.

Layer `values-pre-dev.yaml` over `values-dev.yaml` for Pre-Dev. This keeps both
chart-managed SigNoz UI ingresses off and the UI Service on `ClusterIP`. The
separate browser metrics route stays available. Use the existing Pre-Dev
Kubernetes context to open a loopback connection to SigNoz.

```bash
kubectl --context "$PRE_DEV_KUBE_CONTEXT" -n caritas port-forward \
  --address=127.0.0.1 service/caritas-signoz 18080:8080
```

Open `http://127.0.0.1:18080/signoz/` and sign in with your existing SigNoz
account. Later values and `--set` options take precedence, so check the final
release inputs for any UI exposure overrides.

After an authorized rollout, verify the applied Helm revision, the private
Service and every ingress or gateway route to it. Check `/signoz/`, its
JavaScript asset and the dashboard API from both the public host and loopback.
The public app can return its own HTML for `/signoz/`, so check the service
identity as well as HTTP status. Verify the RUM path independently.
[Pre-Dev operations hardening](https://github.com/OpenResilienceInitiative/ORISO-Helm/issues/304)

Layer `values-prod.yaml` over the base values for production. With that overlay,
`signoz.enabled=true` prepares the internal UI and data stores. The bundled
collector has zero replicas and the overlay disables its autoscaler, public
ingresses, gateway, and Java, infrastructure and browser exporters. The UI and
collector Services use `ClusterIP`. The production overlay uses a named
Argo CD Sync hook for the ClickHouse migration. Use the
[Argo CD migration procedure](runbooks/signoz-managed-observability.md#signoz-migrations-with-argo-cd)
before enabling SigNoz or changing its migration image.

After the activation requirements are verified, local Java export needs
`global.observability.otlpEnabled=true` and a positive
`signoz.otelCollector.replicaCount`. An explicitly configured independent
`global.observability.otlpCollectorHost` can receive Java export with the
bundled collector still dormant. Local infrastructure and browser producers
require a running bundled collector. Browser metrics also require the dedicated
`signoz.otelCollector.ports.otlp-rum.enabled` Service port.

Production activation requires a verified privacy pipeline on the current
collector, retention, backup and restore, and authenticated UI access. The
historical pseudonymization pipeline targeted an earlier collector.
[Historical collector PR](https://github.com/OpenResilienceInitiative/ORISO-Helm/pull/65) · [Production acceptance](https://github.com/OpenResilienceInitiative/ORISO-E2E/issues/99)

### Add-ons (optional)

Add-ons are optional services with their own backend and UI (ADR-034). The
[AddonService](https://github.com/OpenResilienceInitiative/ORISO-AddonService) discovers them,
lets a SuperAdmin enable each one per Träger and serves them under
`/service/addons`. It is off by default (`addonService.enabled: false`); with
it off, nothing below is rendered.

To enable it, set in `values.yaml`:

- `addonService.enabled: true`
- `addonService.image.repository` / `addonService.image.tag`: required, no
  default (a tag of the form `sha256:<digest>` pins by digest);
  `addonService.imagePullSecrets` for a private registry
- `addonService.discovery.namespace`: where add-ons run (default `addons`). The
  chart grants the AddonService `get`/`list` on Services there only. The
  namespace must exist before the install, or set
  `addonService.discovery.createNamespace: true`
- `addonService.discovery.staticAddons`: add-on base URLs used in addition to
  Kubernetes discovery (`discovery.kubernetes: false` turns discovery and its
  RBAC off)

and in `secrets.yaml`:

- `global.secrets.addonServiceDbPassword` (and optionally
  `addonServiceDbUsername`, default `addonservice`). The Job
  `addonservice-db-init-*` creates the `addonservice` database and user as
  MariaDB root on install and upgrade; Liquibase in the service creates the
  tables. Use a password without quotes. After changing it, delete the
  completed Job so the next upgrade runs it again.
- `addonService.tokenPrivateKeyPem`: the RS256 key that signs the tokens
  add-ons receive. It must stay the same across restarts and replicas:

  ```bash
  openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out addonservice-token.pem
  ```

Add-ons are not part of this chart; each brings its own chart or manifests
and installs into the add-on namespace. An add-on:

- exposes a Service labelled `oriso.io/addon: "true"` (port from the
  annotation `oriso.io/addon-port`, otherwise the first port)
- serves its manifest at `/.well-known/oriso-addon.json`
- should accept traffic only from the AddonService, for example:

  ```yaml
  apiVersion: networking.k8s.io/v1
  kind: NetworkPolicy
  metadata:
    name: only-addonservice
    namespace: addons
  spec:
    podSelector: {}
    policyTypes: [Ingress]
    ingress:
      - from:
          - namespaceSelector:
              matchLabels:
                kubernetes.io/metadata.name: caritas
            podSelector:
              matchLabels:
                app: addonservice
  ```

Add-on backends reach the AddonService at
`http://addonservice.<release namespace>.svc:8090`.
