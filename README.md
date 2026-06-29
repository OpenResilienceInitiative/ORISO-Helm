# ORISO Helm Chart

Helm chart for deploying the [ORISO](https://github.com/OpenResilienceInitiative) online counseling platform on Kubernetes.

The chart covers the full stack: Keycloak, MariaDB, MongoDB, RabbitMQ, Redis, Matrix/Synapse, LiveKit, and all backend/frontend services.

## Getting started

### 1. Create your local values file

The repository ships a `values-default.yaml` with placeholder passwords (each password is set to its corresponding username). Copy it and fill in your real credentials:

```bash
cp values-default.yaml values.yaml
```

`values.yaml` is listed in `.gitignore` and will never be committed.

### 2. Set your passwords

Open `values.yaml` and replace all placeholder values with your actual passwords. Fields to look for:

- `global.secrets.*Password` / `*Pass` — database and service passwords
- `global.secrets.matrixRegistrationSharedSecret` — Matrix shared secret
- `global.keycloak.technicalUser.password` — Keycloak technical user
- `postgres.postgresPassword` — PostgreSQL root password
- `matrix.matrixAdminPassword` — Matrix admin password
- `online-counseling-mongodb.*Password` / `*Pass` — MongoDB passwords
- `online-counseling-mariadb.dbRootPassword` — MariaDB root password
- `livekit.api.secret` — LiveKit API secret
- `tenantService.springDatasourcePassword` / `springRabbitmqPassword`
- `userService.rocket*Password` / `keycloakTechnicalPassword`

Also set `global.domainName` (and the derived domain fields) to your actual domain — they are pre-filled with `your.domain` as a placeholder.

Set `global.keycloak.realm` to your Keycloak realm name — it is pre-filled with `your-realm` and appears in several URL fields throughout the file.

### 3. Install / Upgrade

```bash
helm upgrade --install caritas ./ --namespace caritas --create-namespace --wait-for-jobs --timeout 15m -f values.yaml
```

The first `caritas` is the Helm release name, the second is the Kubernetes namespace. Both can be set to whatever fits your environment, they don't have to match.
