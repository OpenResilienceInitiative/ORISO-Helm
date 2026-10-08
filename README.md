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
- `global.taskIdentitySecrets.<TASK_KEY>` — a distinct persistent client secret for every entry in `files/task-identities.json` (14 task identities). Generate independent random credentials and keep them in your managed secrets source. Blank, example and reused values fail rendering.
- `global.commandOriginKeys.{provisioning,maintenance,tenantCreation,wizardPolicy}` — four independent Base64 keys, each at least 32 decoded bytes, for the corresponding signed operation contexts. They must not reuse a task credential.
- `global.taskIdentities.subjects.<TASK_KEY>` — for existing task clients, the verified actual Keycloak service-account UUID. A fresh realm derives deterministic UUIDs; the reconciler refuses identity collisions and verifies native client ownership before changes.
- `postgres.postgresPassword` — PostgreSQL root password
- `global.matrix.matrixAdminUsername` / `matrixAdminPassword` — Matrix admin credentials (must live under `global:` so subcharts can read them)
- `online-counseling-mongodb.*Password` / `*Pass` — MongoDB passwords
- `online-counseling-mariadb.dbRootPassword` — MariaDB root password
- `matrixrtcAuth.membershipReaderPassword` — password for the non-admin MatrixRTC membership reader user that Helm bootstraps in Synapse
- `livekit.api.key` / `livekit.api.secret` — LiveKit API credentials
- `tenantService.springDatasourcePassword` / `springRabbitmqPassword`
- `agencyService.serviceEncryptionAppkey` — AgencyService encryption key (Matrix service-account passwords). **Required** — the chart refuses to render if it is blank, because an empty key silently breaks agency creation. Rotating it invalidates already-stored credentials.
- `userService.serviceEncryptionAppkey` — UserService encryption key. Runtime workloads use their own task credentials; the shared technical password and Keycloak administrator credentials are no longer runtime dependencies.
- `global.secrets.keycloakAdminUsername` / `keycloakAdminPassword` — installation-time Keycloak access used by the Keycloak pod and deployment hooks. Never mount it into application services or the running SMTP synchronizer.

Existing-realm migration is coordinated across Keycloak, UserService, TenantService,
AgencyService and ConsultingTypeService. Review their matching task-contract changes
before deploying the chart. The task reconciler runs **pre-upgrade**, before receiver
pods request their new task tokens; credentials and script hooks run first and remain
available after success. On a fresh installation, native realm import creates the task
clients and reconciliation runs post-install. The realm import is a Kubernetes Secret.

Keep `global.taskIdentities.retireLegacy=false` until the actual consumers of all four
legacy actors have been verified. Existing credentials are retained only if both old
client secrets are explicitly supplied; no old secret is a runtime fallback. Retirement
requires every verified `legacySubjects` UUID and native username/client ownership,
then disables only those actors and removes their grants. Human accounts stay intact.
The optional `legacyOtpCompatibility` bridge defaults false and only supports the
existing verified backend-admin OTP caller; disable it before legacy retirement.
Unknown external mail/appointment authentication and received-mail acceptance remain
release gates. The provider ADR remains Proposed until human technical review.

Before an existing installation switches its invitation/Wizard/cleanup callers,
inventory pending invitations, durable release tasks and the actual tenant/agency
reservation ledgers. Old agency rows and release tasks may have no owner proof.
The narrowed actor deliberately cannot release or consume those rows; new-proof
tests do not establish that old cancellation/expiry still works.

1. Match the original invitation, exact reserved ID, tenant, current unconsumed
   receiver record and genuine captured token. A verified existing token may be
   migrated with its original ownership; never invent a token or infer an owner
   from an ID alone, and never log reservation proofs.
2. A receiver row with no genuine proof needs an operator-owned resolution. Drain
   or reissue it only after verifying the unit is unassigned, identifying its
   original owner and checking every live dependent invite; capture only the
   receiver-generated fresh proof on the matching invite/release record.
3. Keep the affected caller cutover and legacy retirement gated until these rows
   are resolved and representative old invitation acceptance, cancellation and
   expiry pass. Unknown counts or unverifiable ownership keep this gate open.
   There is no shared technical/admin fallback or automatic guessed backfill.

For developers — isolated permission checks use synthetic credentials only:

```bash
python3 tests/keycloak_task_migration_test.py
python3 tests/task_receiver_permission_matrix.py \
  --receiver ../ORISO-TenantService \
  --receiver ../ORISO-AgencyService \
  --receiver ../ORISO-ConsultingTypeService \
  --receiver ../ORISO-UserService \
  --java-home "$JAVA_HOME"
```

Use the matching integration branches, Maven, Docker and Java 21. The receiver run
starts its own native Keycloak container, sends actual issued bearer tokens through
normal resource-server decoders and HTTP endpoints, and checks signed wrong-subject,
wrong-audience and unrelated-grant denials. It removes its container/private temporary
fixture; it does not change a deployed realm or send real email. The harness requires a
fresh executed suite with zero failures, errors and skips; Maven exit alone is not
permission evidence.

The required cross-repository CI job pins the receiver and custom-provider source
commits and also runs the actual UserService command adapter plus its file-backed
journal through native provider restart and process-death recovery. To reproduce
that second gate after building the matching custom image:

```bash
python3 ../ORISO-Keycloak/scripts/test-task-command-permissions.py \
  --image oriso-keycloak:permission-contract \
  --userservice-receiver ../ORISO-UserService \
  --userservice-java-home "$JAVA_HOME"
```

Ordinary test selection excludes the fixture-dependent native classes; these
explicit joined runs must execute them. This remains local/CI evidence, separate
from human review, deployment, browser completion and received email.

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
