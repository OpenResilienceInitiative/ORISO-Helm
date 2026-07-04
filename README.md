# ORISO Helm Chart

Helm chart for deploying the [ORISO](https://github.com/OpenResilienceInitiative) online counseling platform on Kubernetes.

The chart covers the full stack: Keycloak, MariaDB, MongoDB, RabbitMQ, Redis, Matrix/Synapse, LiveKit, and all backend/frontend services.

> **Liquibase re-enablement (2026):** before an environment is switched back to `SPRING_LIQUIBASE_ENABLED=true`, it needs a one-time database baseline — see [runbooks/liquibase-baseline-sync.md](runbooks/liquibase-baseline-sync.md).

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
- `matrix.matrixAdminPassword` — Matrix admin password
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
