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
- MatrixRTC and LiveKit runtime credentials are not Helm values; provision the
  external Kubernetes Secrets described below before rendering or deploying.
- `tenantService.springDatasourcePassword` / `springRabbitmqPassword`
- `agencyService.serviceEncryptionAppkey` — AgencyService encryption key (Matrix service-account passwords). **Required** — the chart refuses to render if it is blank, because an empty key silently breaks agency creation. Rotating it invalidates already-stored credentials.
- `userService.keycloakTechnicalPassword` / `serviceEncryptionAppkey` / `identityTechnicalUser*`

### 3. Install / Upgrade

For the coordinated Matrix-only/Matryoshka cutover, never edit image tags into
the generated release overlay. Frontend, Element Call, UserService,
AgencyService, LiveKit, Synapse and both MatrixRTC authorization images accept only complete
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

Before deployment, capture the current Helm revision, exact before/target image
sets, and the bounded rollback command without reading or exporting Secrets:

```bash
./scripts/capture-cutover-rollback.py \
  --release caritas \
  --namespace caritas \
  --target-values /path/to/new-cutover-digests.yaml \
  --output-dir /path/to/new-cutover-evidence
```

The capture also compares the live Deployment images with the images stored in
that Helm revision. It fails when host-side `kubectl set image` updates or
mutable tags have made the release history diverge from the live cluster. In
that case, first perform a reviewed baseline Helm upgrade with the current
application binaries expressed as immutable digests, verify it, and rerun the
capture. Only a revision that exactly describes the live image set is a valid
atomic rollback target.

Use the verified overlay after the environment values and before the remaining
non-Matrix environment values. MatrixRTC and LiveKit runtime credentials must
already exist as external Kubernetes Secrets:

```bash
helm upgrade --install caritas ./ --namespace caritas --create-namespace \
  --atomic --wait --wait-for-jobs --timeout 15m \
  -f values.yaml.default \
  -f /path/to/environment-values.yaml \
  -f /path/to/new-cutover-digests.yaml \
  -f /path/to/environment-secrets.yaml
```

The first `caritas` is the Helm release name, the second is the Kubernetes namespace. Both can be changed to suit your environment.

### MatrixRTC / LiveKit runtime Secrets

LiveKit and MatrixRTC auth read sensitive runtime material only from
pre-provisioned Kubernetes Secrets. The chart deliberately renders neither
Secret, so their contents cannot enter `helm template` output or Helm release
history.

Before installation, provision `matrixrtcAuth.existingSecret.name` (default
`matrixrtc-auth-runtime`) with these keys:

- `matrix-membership-token` (a real token for the dedicated non-admin reader)
- `call-policy-token` (a dedicated high-entropy value shared with UserService)
- `livekit-api-key` and `livekit-api-secret`
- `redis-url` (the complete authenticated Redis URL)

Also provision `livekit.existingConfigSecret.name` (default
`livekit-config-runtime`) with `config.yaml`. Supply secret data through files
or the environment's secret controller; do not put it on a command line or in
a Helm values file.

Create the non-admin membership-reader identity and obtain its Matrix access
token through the environment's protected provisioning process before
installation. Helm deliberately cannot register users, read the Synapse
registration secret, or patch runtime Secrets. MatrixRTC auth waits for a
valid token before becoming Ready.

For an existing release that used Helm-rendered `matrixrtc-auth-secrets` and
`livekit-config`, first copy their data into the new external Secret names and
verify every required key. Then deploy the chart that references them. This name transition
prevents Helm from deleting the new runtime Secrets when it prunes the old
managed objects. Remove the old Secrets only after the cutover is verified.

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

### Prod telemetry (OTLP → SigNoz)

Prod telemetry export is off by default unless the bundled SigNoz dependency
is enabled. Set `signoz.enabled=true` to deploy SigNoz with ORISO-Helm and
automatically point the backend services at the in-cluster OTLP HTTP collector
(`caritas-signoz-otel-collector.<namespace>:4318` for the default release
name):

```bash
helm dependency build .
helm upgrade --install caritas ./ -n caritas --create-namespace \
  --wait-for-jobs --timeout 15m \
  -f values.yaml -f secrets.yaml \
  --set signoz.enabled=true
```

The SigNoz UI is exposed at `https://signoz.<global.domainName>` by the parent
chart ingress. If SigNoz is deployed somewhere else, leave `signoz.enabled=false`
and set both `global.observability.otlpEnabled=true` and
`global.observability.otlpCollectorHost=<collector-host>:4318`. No
`secrets.yaml` change is required for the bundled default SigNoz install.

The KDG-safe pseudonymization pipeline that would make turning production
telemetry on safe is built but also off by default
(`global.observability.telemetryPseudonymizationEnabled`) — see
`docs/observability-prod-pseudonymization.md` for exactly what is
pseudonymized/dropped and the sign-off steps before either flag is flipped
for prod.
