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

- `global.domainName` — your public domain (and the derived `domains.*` / URL fields)
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
- `global.keycloak.serviceTechUserId` — Keycloak technical user ID
- `postgres.postgresPassword` — PostgreSQL root password
- `global.matrix.matrixAdminUsername` / `matrixAdminPassword` — Matrix admin credentials (must live under `global:` so subcharts can read them)
- `online-counseling-mongodb.*Password` / `*Pass` — MongoDB passwords
- `online-counseling-mariadb.dbRootPassword` — MariaDB root password
- `matrixrtcAuth.membershipReaderPassword` — password for the non-admin MatrixRTC membership reader user that Helm bootstraps in Synapse
- `livekit.api.key` / `livekit.api.secret` — LiveKit API credentials
- `tenantService.springDatasourcePassword` / `springRabbitmqPassword`
- `agencyService.serviceEncryptionAppkey` — AgencyService encryption key (Matrix service-account passwords). **Required** — the chart refuses to render if it is blank, because an empty key silently breaks agency creation. Rotating it invalidates already-stored credentials.
- `userService.keycloakTechnicalPassword` / `serviceEncryptionAppkey` / `identityTechnicalUser*`

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
helm upgrade --install caritas ./ --namespace caritas --create-namespace --wait-for-jobs --timeout 15m -f secrets.yaml
```

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

During `helm upgrade --install`, the chart creates `matrixrtc-auth-secrets` and
`livekit-config`, then a `matrixrtc-bootstrap-token` Job waits for Synapse,
creates or reuses the membership reader user, logs in, and patches the real
Matrix access token into `matrixrtc-auth-secrets`. MatrixRTC auth waits for
that token before starting, so LiveKit, MatrixRTC auth, and Element Call can
start without a second bootstrap step.

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

### Prod telemetry (OTLP → SigNoz)

Prod telemetry export is off by default (`global.observability.otlpEnabled:
false` in `values-prod.yaml`) and stays off until a human explicitly decides
otherwise. The KDG-safe pseudonymization pipeline that would make turning it
on safe is built but also off by default
(`global.observability.telemetryPseudonymizationEnabled`) — see
`docs/observability-prod-pseudonymization.md` for exactly what is
pseudonymized/dropped and the sign-off steps before either flag is flipped
for prod.
