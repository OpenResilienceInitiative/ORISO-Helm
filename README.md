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
- `livekit.api.key` / `livekit.api.secret` — LiveKit API credentials
- `tenantService.springDatasourcePassword` / `springRabbitmqPassword`
- `userService.rocket*Password` / `keycloakTechnicalPassword` / `serviceEncryptionAppkey`

### 3. Install / Upgrade

```bash
helm upgrade --install caritas ./ --namespace caritas --create-namespace --wait-for-jobs --timeout 15m -f secrets.yaml
```

The first `caritas` is the Helm release name, the second is the Kubernetes namespace. Both can be changed to suit your environment.

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
