# HealthDashboard ingress authentication

This chart change covers only the HealthDashboard route in
[ORISO-Helm #304](https://github.com/OpenResilienceInitiative/ORISO-Helm/issues/304).
The wider observability access review and DPIA annex remain open. Local chart
renders do not establish that any environment has received this change.

## Configuration and upgrade impact

The HealthDashboard Deployment and internal Service continue to render.
Its external Ingress is disabled by default. A later upgrade using the new base
values removes the chart-managed `/health` route unless the environment
explicitly enables it. Existing customized values should be reviewed before
upgrading. PreDev and production overlays do not opt in.

Every enabled ingress requires Basic Authentication; there is no separate switch
to disable authentication. The Basic Auth realm is fixed as
`HealthDashboard Authentication`. Configuration contains only an existing Secret name:

```yaml
healthDashboard:
  ingress:
    enabled: true
    authSecret: health-dashboard-basic-auth
```

The Dev overlay opts in using this name. It does not provision the Secret or
prove that the Secret exists. Leading/trailing whitespace is trimmed; a missing,
empty or whitespace-only name stops Helm rendering. Namespace-qualified names
containing `/` are also rejected.

Before an authorized rollout, the operator must provision the named Secret
through the normal secret-management process. It must contain an
nginx-compatible htpasswd file under the `auth` key. Do not put credentials or
htpasswd data in chart values, Git, logs or review evidence, and do not reuse or
rotate Storybook/Redis credentials as part of this change.

The Secret must be in the **effective ingress namespace**:
`healthDashboard.namespace` when nonempty, otherwise the Helm release namespace.
The Ingress, Service and Deployment use that same namespace. Supply a local
Secret name, never a `namespace/name` reference. Helm rendering does not check
Secret existence, contents or ingress-controller configuration. A missing,
malformed or wrong-namespace Secret can make the route unavailable.

## Local verification

These checks use synthetic fixture values and do not contact a cluster:

```sh
python3 tests/render_health_dashboard_ingress_auth_test.py
python3 tests/render_storybook_dev_test.py
python3 tests/render_environment_overlays_test.py
helm lint . -f values.yaml.default -f secrets.yaml.default -f values-dev.yaml \
  --set-string userService.smtpUser=smtp-validation-user \
  --set-string userService.smtpPassword=smtp-validation-password
helm template health-auth-test . --namespace render-test \
  -f values.yaml.default -f secrets.yaml.default -f values-dev.yaml \
  --set-string global.domainName=health.example.test \
  --set-string userService.smtpUser=smtp-validation-user \
  --set-string userService.smtpPassword=smtp-validation-password \
  > /tmp/health-auth-rendered.yaml
```

Check the rendered HealthDashboard Ingress for `auth-type: basic`, the expected
Secret/realm, host, TLS, `/health(/|$)(.*)`, `/$2` rewrite and Service backend.
The automated render tests also cover disabled/default behavior, invalid names,
namespace/port overrides, and Dev/PreDev/production overlays. Overlay names here
identify local inputs, not tested running environments. This change adds no PR
validation workflow; local results must be attached separately to the PR.

## Verification after authorized Dev rollout

The operator must record the Helm revision/chart and deployed HealthDashboard
image separately, then verify the real ingress boundary:

- [ ] Unauthenticated requests to `/health`, `/health/`, and the dashboard's
  actual data endpoints are denied with a Basic Auth challenge (`401`, allowing
  an HTTPS redirect first). Follow redirects and verify the final response.
- [ ] Invalid credentials are denied; valid credentials open the actual
  dashboard and allow its data requests. A generic application fallback or an
  unrelated HTTP `200` is not proof.
- [ ] The internal Service still reaches HealthDashboard successfully.
- [ ] An authenticated browser loads and refreshes the dashboard successfully.

These runtime checks require an authorized rollout and credentials. They are
not completed by rendering, linting or packaging the chart.

## Safe rollback

If the protected route is unavailable, keep authentication in place while the
operator checks the Secret name, `auth` entry and effective namespace. If access
must be withdrawn, set `healthDashboard.ingress.enabled: false` in the approved
environment values and apply it through the normal deployment process. Put
this override after `values-dev.yaml` so it wins. The internal Deployment and
Service remain available, but the public dashboard route is removed.

Do not blindly roll back to an older chart revision: it may recreate the
unauthenticated public route. Any rollback to such a revision must first have a
reviewed external access block, and the operator must verify the live boundary
afterwards. Never recover availability by removing the auth annotations or
creating an unauthenticated replacement ingress.
