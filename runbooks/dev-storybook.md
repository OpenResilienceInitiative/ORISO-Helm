# Dev Storybook

The Frontend and Admin workflows publish the current `dev` component
documentation as `ghcr.io/openresilienceinitiative/oriso-storybook:dev`. This
chart serves the frontend and admin Storybook workloads at
`https://dev.oriso.org/storybook-frontend/` and
`https://dev.oriso.org/storybook-admin/` when the Dev overlay is applied.

Storybook contains internal development documentation, so its ingress requires
HTTP Basic Authentication. Before the Helm upgrade, create or rotate the
`storybook-basic-auth` Secret in the release namespace through the normal secret
management process. The Secret must contain an nginx-compatible `auth` htpasswd
entry. Do not commit credentials or generated htpasswd data.

The Basic Auth realm is fixed to `ORISO Storybook`. The legacy
`storybook.ingress.authRealm` value is deprecated and ignored, including custom
labels: nginx treats `off` as disabling Basic Auth, and variable expressions can
resolve to `off`. Existing values files remain accepted; the enabled switch,
Secret reference and routes keep their existing behavior.

Verify the rendered chart before deployment:

```sh
python3 tests/render_storybook_dev_test.py
python3 tests/render_storybook_auth_realm_test.py
helm lint . -f values.yaml.default -f secrets.yaml.default -f values-dev.yaml
```

Install or upgrade the Dev overlay from a checkout of this chart:

```sh
cp values.yaml.default values.yaml
# Configure values.yaml for Dev, including global.domainName, Keycloak realm,
# Keycloak URLs, and Matrix server names.
# Prepare secrets.yaml through the normal secret-management process.

helm upgrade --install caritas . \
  -n caritas \
  --create-namespace \
  --wait-for-jobs \
  --timeout 15m \
  -f values.yaml \
  -f secrets.yaml \
  -f values-dev.yaml
```

After deployment, verify the boundary and the current story:

1. An unauthenticated request to `/storybook-frontend/` returns `401`.
2. Requests to `/storybook-frontend` and `/storybook-admin` redirect to the
   trailing-slash routes.
3. An authenticated browser opens both Storybook indexes.
4. Open the Frontend self-help group Owner and Participant stories and confirm their
   interaction tests pass.
5. Record the deployed Storybook image digest separately from the Helm release.

## Storybook MCP for coding agents

Coding agents read the component documentation through the Storybook MCP
(`@storybook/addon-mcp`, tools such as `docs-list` and `docs-show`). The static
builds above cannot answer `/mcp`, so `storybook.mcp.enabled` adds one
`storybook dev` server per app:

| App | MCP endpoint |
|---|---|
| Frontend | `https://dev.oriso.org/storybook-frontend-mcp/mcp` |
| Admin | `https://dev.oriso.org/storybook-admin-mcp/mcp` |

Each pod clones the configured branch (`storybook.mcp.branch`, default `dev`)
at start, installs dependencies and starts Storybook. Startup takes several
minutes. To pick up new commits, restart the Deployments:

```sh
kubectl -n caritas rollout restart deployment/storybook-mcp-frontend deployment/storybook-mcp-admin
kubectl -n caritas logs deployment/storybook-mcp-frontend | grep 'source revision'
```

The route uses its own basic-auth Secret, `storybook-mcp-basic-auth` (an
nginx-compatible `auth` htpasswd entry for user `storybook`). Create it through
the normal secret-management process before the Helm upgrade and keep the
plain credential in the team secret store. Do not commit it.

Verify after deployment:

1. An unauthenticated `POST` to either endpoint returns `401`.
2. An authenticated JSON-RPC `initialize` returns `200` with `serverInfo`.
3. `tools/list` contains `docs-list` and `docs-show`.

Register the servers in Claude Code (user scope) with the credential from the
secret store, never typed into a shared file:

```sh
AUTH="Basic $(printf 'storybook:%s' "$STORYBOOK_MCP_PASSWORD" | base64)"
claude mcp add --scope user --transport http storybook-frontend \
  https://dev.oriso.org/storybook-frontend-mcp/mcp --header "Authorization: $AUTH"
claude mcp add --scope user --transport http storybook-admin \
  https://dev.oriso.org/storybook-admin-mcp/mcp --header "Authorization: $AUTH"
```

If a default-deny NetworkPolicy is enabled in the namespace, these pods need
egress to `github.com` and `registry.npmjs.org`.
