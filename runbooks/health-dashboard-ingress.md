# HealthDashboard ingress authentication

This chart change covers only the HealthDashboard route in
[ORISO-Helm #304](https://github.com/OpenResilienceInitiative/ORISO-Helm/issues/304).
The wider observability access review and DPIA annex remain open. Local chart
renders do not establish that any environment has received this change.

## Configuration and upgrade impact

The HealthDashboard Deployment and internal Service continue to render.
The base chart enables the external Ingress with Basic Authentication. The Dev,
PreDev, and production overlays keep it enabled. Review the effective values
before an upgrade. To keep the dashboard internal, put the following in a values
file and pass that file after the environment overlay.

```yaml
healthDashboard:
  ingress:
    enabled: false
```

Alternatively, pass `--set healthDashboard.ingress.enabled=false` to Helm.

Basic Authentication is enabled by default for the external route. Keep it
enabled for a restricted dashboard. The realm is fixed as
`HealthDashboard Authentication`. The values reference the Secret by name:

```yaml
healthDashboard:
  ingress:
    enabled: true
    basicAuth: true
    authSecret: health-dashboard-basic-auth
```

The Dev overlay opts in using this name. It does not provision the Secret or
prove that the Secret exists. Leading/trailing whitespace is trimmed; a missing,
empty or whitespace-only name stops Helm rendering. Namespace-qualified names
containing `/` are also rejected. The trimmed name must follow Kubernetes
Secret-name rules: at most 253 lowercase ASCII letters, digits, hyphens and dots,
with each nonempty dot-separated segment beginning and ending in a letter or
digit. Numeric starts and segments longer than 63 characters are valid within
the 253-character total limit. Invalid names stop Helm rendering.

Before an authorized rollout, the operator must provision the named Secret
through the normal secret-management process. It must contain an
nginx-compatible htpasswd file under the `auth` key. Do not put credentials or
htpasswd data in chart values, Git, logs or review evidence, and do not reuse or
rotate Storybook/Redis credentials as part of this change.

The Secret must be in the **effective ingress namespace**:
`healthDashboard.namespace` when nonempty, otherwise the Helm release namespace.
The Ingress, Service and Deployment use that same namespace. Supply a local
Secret name, never a `namespace/name` reference. Offline rendering validates the
name. When Helm can look up an existing namespace during an upgrade and the
chart does not render an htpasswd Secret, the template also checks that the
referenced Secret exists. Verify the `auth` entry and the ingress controller
before rollout. A malformed or wrong-namespace Secret can make the route
unavailable.

## Public status view

The public status view remains available without login as required by
[HealthDashboard #57](https://github.com/OpenResilienceInitiative/ORISO-HealthDashboard/issues/57).
Before selecting that mode, verify that the installed image includes the
public-mode changes and that `ORISO_PUBLIC_MODE` is enabled. Set
`healthDashboard.ingress.basicAuth` to `false` as a boolean in the environment
overlay. To keep public mode enabled, omit
`healthDashboard.env.ORISO_PUBLIC_MODE` to use the default or set it to
`"true"`. With Basic Authentication disabled, the chart rejects both the YAML
boolean `false` and the string `"false"` for that value.
The public view omits the Helm image inventory, internal service URLs and
raw health responses. Operator-only routes stay unavailable while role-based
access is tracked in
[HealthDashboard #58](https://github.com/OpenResilienceInitiative/ORISO-HealthDashboard/issues/58).

## Local verification

These checks use synthetic fixture values and do not contact a cluster:

```sh
python3 tests/render_health_dashboard_ingress_auth_test.py
python3 tests/render_storybook_dev_test.py
python3 tests/render_environment_overlays_test.py
helm lint . -f values.yaml.default -f tests/fixtures/values-render-domain.yaml \
  -f secrets.yaml.default -f tests/fixtures/render-required-secrets.yaml -f values-dev.yaml \
  --set-string userService.smtpUser=smtp-validation-user \
  --set-string userService.smtpPassword=smtp-validation-password
helm template health-auth-test . --namespace render-test \
  -f values.yaml.default -f tests/fixtures/values-render-domain.yaml \
  -f secrets.yaml.default -f tests/fixtures/render-required-secrets.yaml -f values-dev.yaml \
  --set-string userService.smtpUser=smtp-validation-user \
  --set-string userService.smtpPassword=smtp-validation-password \
  > /tmp/health-auth-rendered.yaml
```

Check the rendered HealthDashboard Ingress for `auth-type: basic`, the expected
Secret/realm, host, TLS, `/health(/|$)(.*)`, `/$2` rewrite and Service backend.
The automated render tests cover the authenticated default, explicit disablement,
invalid names, namespace and port overrides, and Dev, PreDev, and production
overlays. They also verify that the public ingress requires public mode.
Record local and deployed verification separately in the PR.

## Verification after authorized Dev rollout

Record the Helm chart revision and deployed HealthDashboard image separately.
Use the authorized chart deployment path for ingress changes. Updating the Dev
image alone leaves the installed Ingress unchanged. For a restricted dashboard,
verify the real ingress boundary:

- [ ] Unauthenticated requests to `/health`, `/health/`, and the dashboard's
  actual data endpoints are denied with a Basic Auth challenge (`401`, allowing
  an HTTPS redirect first). Follow redirects and verify the final response.
- [ ] Invalid credentials are denied; valid credentials open the actual
  dashboard and allow its data requests. A generic application fallback or an
  unrelated HTTP `200` is not proof.
- [ ] The internal Service still reaches HealthDashboard successfully.
- [ ] An authenticated browser loads and refreshes the dashboard successfully.

For the approved public status view, confirm that a signed-out browser loads
status information and that the deployed image withholds operator information.
Verify the public and restricted views through their respective acceptance
checks after rollout. Chart rendering and lint cover configuration only.

## Safe rollback

If the protected route is unavailable, keep authentication in place while the
operator checks the Secret name, `auth` entry and effective namespace. If access
must be withdrawn, use the [ingress override](#configuration-and-upgrade-impact)
through the normal deployment process. When applying a values file on Dev, pass
it after `values-dev.yaml`. The internal Deployment and
Service remain available, but the public dashboard route is removed.

Do not blindly roll back to an older chart revision: it may recreate the
unauthenticated public route. Any rollback to such a revision must first have a
reviewed external access block, and the operator must verify the live boundary
afterwards. For a restricted dashboard, recover availability with the required
authentication in place or withdraw the public route.
