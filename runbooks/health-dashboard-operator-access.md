# Keycloak access to HealthDashboard image inventory

The public dashboard continues to show service status. The Helm image inventory
requires a Keycloak account with the `view-helm-images` client role on
`health-dashboard`. Use the dashboard image that implements this authorization
before enabling the Helm values. The release and acceptance are tracked in
[HealthDashboard #58](https://github.com/OpenResilienceInitiative/ORISO-HealthDashboard/issues/58).

## Configure the realm

For a new realm, the chart renders the public `health-dashboard` client and its
role in the realm ConfigMap. For an existing realm, Keycloak skips realm
import on startup. Configure the client and the role in the existing realm
through the approved Keycloak admin process before enabling the dashboard.
Use the dashboard client entry, role and role scope mapping from the rendered
ConfigMap as the reference. Importing a whole realm can affect other clients.

The client uses the standard authorization code flow with S256 PKCE and the
installation's own HTTPS origin. Use the exact callback at
`/health/auth/callback` and logout redirect at `/health/`. The access token
must carry the `health-dashboard` audience. The only dashboard role in scope
is `view-helm-images`. Assign it individually to approved operators.

## Enable and verify

Use these values with the approved dashboard image and the normal Helm delivery
process.

```yaml
global:
  healthDashboardOperatorAccess: true
healthDashboard:
  ingress:
    enabled: true
    basicAuth: false
```

The chart derives the Keycloak issuer from the installation origin and realm.
Use a signed-out browser to verify public status. Sign in with a permitted
operator and verify that Helm Images loads. Sign in with an account without the
role and verify that services are still visible and the inventory is
unavailable. After logout, verify that the inventory is cleared. Record the
installed chart and image revisions with these results.
