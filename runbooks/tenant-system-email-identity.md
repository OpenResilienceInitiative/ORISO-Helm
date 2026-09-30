# Tenant-owned system mail: service identity pins

TenantService owns tenant SMTP passwords. Its constrained internal delivery
endpoint accepts a signed Keycloak access token only when its `sub` and `azp`
match the configured pins and it has the realm role `technical`. The endpoint
does not provide a platform SMTP fallback.

The Helm values are `tenantService.systemEmailDeliveryServiceSubject` and
`tenantService.systemEmailDeliveryServiceClient`. They render only into the
TenantService ConfigMap and container environment. They default to empty,
which makes TenantService deny every delivery request. This allows a new
installation in default PLATFORM mode without guessing a Keycloak user ID;
OWN-server delivery remains unavailable until the operator sets both pins.

## Before enabling OWN-server delivery

1. Identify the exact technical account that UserService uses for its
   `IDENTITY_TECHNICAL_USER_USERNAME` login in this environment. In the
   Keycloak Admin Console, read that user's ID from the existing realm. Do
   not copy the sample ID in `realm.json`: an existing realm may have a
   different ID.
2. Confirm the access token returned to that account has the same `sub`,
   `azp` equal to the UserService login client (`app` in the current source),
   and the realm role `technical`. Inspect the token locally; do not put it in
   an issue, chart values, logs, or a screenshot.
3. Set the two Helm values in the environment's operator-controlled values
   file. The subject is the actual token `sub`; the client is its actual
   `azp`. Keep the pair separate for Dev and Stage.
4. Render the chart and confirm both keys appear in
   `tenantservice-configmap-env` and the TenantService Deployment references
   them. After deployment, test that an ordinary end-user token and a token
   with a wrong subject or client are denied. Then send a test notification
   for a complete OWN tenant and confirm its own server delivered it.
5. Leave either pin blank to disable this route again. Do not choose an
   arbitrary replacement identity or silently switch an OWN tenant to
   PLATFORM. UserService must report a configuration error for an incomplete
   OWN path.

These pins authorize a narrow TenantService endpoint. They are not SMTP
credentials and do not belong in the browser or tenant read APIs.
