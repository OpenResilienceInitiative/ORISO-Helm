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

Verify the rendered chart before deployment:

```sh
python3 tests/render_storybook_dev_test.py
helm lint . -f values.yaml.default -f secrets.yaml.default -f values-dev.yaml
```

After deployment, verify the boundary and the current story:

1. An unauthenticated request to `/storybook-frontend/` returns `401`.
2. Requests to `/storybook-frontend` and `/storybook-admin` redirect to the
   trailing-slash routes.
3. An authenticated browser opens both Storybook indexes.
4. Open the Frontend self-help group Owner and Participant stories and confirm their
   interaction tests pass.
5. Record the deployed Storybook image digest separately from the Helm release.
